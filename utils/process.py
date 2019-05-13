#!/usr/bin/env python
# Copyright (C) 2010-2013 Claudio Guarnieri.
# Copyright (C) 2014-2016 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import sys
import time
import logging
import argparse
import signal
import multiprocessing
import traceback
from functools import partial

# from pathos.helpers import ThreadPool
from multiprocessing.pool import ThreadPool

from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))

from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.core.database import Database, TASK_REPORTED, TASK_COMPLETED
from lib.cuckoo.core.database import Task, TASK_FAILED_PROCESSING
from lib.cuckoo.core.plugins import RunProcessing, RunSignatures, RunReporting
from lib.cuckoo.core.startup import init_modules, drop_privileges

log = None

# We keep a reporting queue with at most a few hundred entries.
QUEUE_THRESHOLD = 128


def process(target=None, copy_path=None, task=None, report=False, auto=False, profile=False):
    if profile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()
    results = RunProcessing(task=task).run()
    if profile:
        profiler.disable()
        profiler.dump_stats(str(task["id"])+".pstat")
    RunSignatures(results=results).run()

    if report:
        RunReporting(task=task, results=results).run()

        if auto:
            if cfg.cuckoo.delete_original and os.path.exists(target):
                os.unlink(target)

            if cfg.cuckoo.delete_bin_copy and copy_path and \
                    os.path.exists(copy_path):
                os.unlink(copy_path)


def process_wrapper(*args, **kwargs):
    try:
        process(*args, **kwargs)
    except Exception as e:
        e.traceback = traceback.format_exc()
        raise e


def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def autoprocess(parallel=8):
    maxcount = cfg.cuckoo.max_analysis_count
    count = 0
    db = Database()
    pending_results = {}
    last_seen = datetime.now()
    # Respawn a worker process every 1000 tasks just in case we
    # have any memory leaks.
    pool = multiprocessing.Pool(processes=parallel, initializer=init_worker,
                                maxtasksperchild=1)

    try:
        while True:
            # Pending results maintenance.
            # log.info("COUNT== #%d", count)
            # log.info("PENDING_RESULTS== #%d", len(pending_results))
            # log.info("LAST SEEN == {0}".format(last_seen.strftime("%Y-%m-%d %H:%M:%S")))

            #TODO: Need a cleaner way to do this and find out why we are hanging in the first place
            if len(pending_results) > 0 and last_seen < (datetime.now()-timedelta(seconds=int(cfg.processing.processing_timeout))):
                log.fatal("Processing is hung, exiting and letting cuckoomonitor start again")
                pool.terminate()
                sys.exit(1)

            for tid, ar in pending_results.items():
                if not ar.ready():
                    continue

                last_seen = datetime.now()
                if ar.successful():
                    log.info("Task #%d: reports generation completed", tid)
                    db.set_status(tid, TASK_REPORTED)
                else:
                    try:
                        ar.get()
                    except Exception as e:
                        log.critical("Task #%d: exception in reports generation: %s", tid, e)
                        if hasattr(e, "traceback"):
                            log.info(e.traceback)

                    db.set_status(tid, TASK_FAILED_PROCESSING)

                pending_results.pop(tid)
                count += 1

            # Make sure our queue has plenty of tasks in it.
            if len(pending_results) >= QUEUE_THRESHOLD:
                time.sleep(1)
                continue

            # End of processing?
            if maxcount and count == maxcount:
                break

            # No need to submit further tasks for reporting as we've already
            # gotten to our maximum.
            if maxcount and count + len(pending_results) == maxcount:
                time.sleep(1)
                continue

            # Get at most queue threshold new tasks. We skip the first N tasks
            # where N is the amount of entries in the pending results list.
            # Given we update a tasks status right before we pop it off the
            # pending results list it is guaranteed that we skip over all of
            # the pending tasks in the database and no further.
            if maxcount:
                limit = maxcount - count - len(pending_results)
            else:
                limit = QUEUE_THRESHOLD

            tasks = db.list_tasks(status=TASK_COMPLETED,
                                  offset=len(pending_results),
                                  limit=min(limit, QUEUE_THRESHOLD),
                                  order_by=Task.completed_on)

            # No new tasks, we can wait a small while before we query again
            # for new tasks.
            if not tasks:
                time.sleep(2)
                continue

            for task in tasks:
                if task.id in pending_results:
                    continue

                log.info("Task #%d: queueing for reporting", task.id)

                if task.category == "file":
                    sample = db.view_sample(task.sample_id)
                    if not sample:
                        log.critical("Task {0}: Sample {1} not found in db".format(task.id, task.sample_id))
                        db.set_status(task.id, TASK_FAILED_PROCESSING)
                        continue
                    copy_path = os.path.join(CUCKOO_ROOT, "storage",
                                             "binaries", sample.sha256)
                else:
                    copy_path = None

                args = task.target, copy_path
                kwargs = {
                    "report": True,
                    "auto": True,
                    "task": dict(task.to_dict()),
                }
                abortable_func = partial(abortable_worker, process_wrapper,orig_kwargs = kwargs, timeout=cfg.processing.processing_timeout, task_id=task.id)
                result = pool.apply_async(abortable_func, args, kwargs)
                if len(pending_results) == 0:
                    # reset the loop protection timer as we are adding a new item after a while
                    last_seen = datetime.now()
                pending_results[task.id] = result
    except KeyboardInterrupt:
        pool.terminate()
        raise
    except:
        log.exception("Caught exception in processing loop")
    finally:
        pool.close()
        pool.join()


def abortable_worker(func, *args, **kwargs):
    log.info("Timeout was passed as {0}".format(kwargs.get('timeout', None)))
    timeout = kwargs.get('timeout', 600)
    task_id = kwargs.get('task_id', "Not specified")
    # db = kwargs.get('database_conn', None)
    # pending_results = kwargs.get('pending_results', None)
    p = ThreadPool(1)
    res = p.apply_async(func, args=args, kwds=kwargs.get("orig_kwargs",None))
    try:
        out = res.get(timeout)  # Wait timeout seconds for func to complete.
        p.close()
        p.join()
        return out
    except multiprocessing.TimeoutError:
        log.critical("Task#{0}: Aborting due to timeout".format(task_id))
        p.terminate()
        raise Exception("Task#{0} timed out".format(task_id))
        # db.set_status(task_id,TASK_FAILED_PROCESSING)
        # pending_results.pop(task_id)



def main():
    global log

    parser = argparse.ArgumentParser()
    parser.add_argument("id", type=str, help="ID of the analysis to process (auto for continuous processing of unprocessed tasks).")
    parser.add_argument("-d", "--debug", help="Display debug messages", action="store_true", required=False)
    parser.add_argument("-r", "--report", help="Re-generate report", action="store_true", required=False)
    parser.add_argument("-p", "--parallel", help="Number of parallel threads to use (auto mode only).", type=int, required=False, default=1)
    parser.add_argument("-u", "--user", type=str, help="Drop user privileges to this user")
    parser.add_argument("-m", "--modules", help="Path to signature and reporting modules - overrides default modules path.", type=str, required=False)
    parser.add_argument("--profile", help="Profile and save results as <report_id>.pstat", action="store_true", required=False)

    args = parser.parse_args()

    if args.user:
        drop_privileges(args.user)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    log = logging.getLogger("cuckoo.process")

    if args.modules:
        sys.path.insert(0, args.modules)

    init_modules(machinery=False)

    if args.id == "auto":
        autoprocess(parallel=args.parallel)
    else:
        task = Database().view_task(int(args.id))
        if not task:
            task = {
                "id": int(args.id),
                "category": "file",
                "target": "",
                "options": "",
            }
            process(task=task, report=args.report, profile=args.profile)
        else:
            process(task=task.to_dict(), report=args.report, profile=args.profile)

if __name__ == "__main__":
    cfg = Config()

    try:
        main()
    except KeyboardInterrupt:
        pass
