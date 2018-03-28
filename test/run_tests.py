#!/usr/bin/env python

import sys
import shutil
import os
import select
import unittest
import argparse
import time
from multiprocessing import Process, Pipe
from framework import VppTestRunner
from debug import spawn_gdb
from log import global_logger
from discover_tests import discover_tests
from subprocess import check_output, CalledProcessError
from util import check_core_path

# timeout which controls how long the child has to finish after seeing
# a core dump in test temporary directory. If this is exceeded, parent assumes
# that child process is stuck (e.g. waiting for shm mutex, which will never
# get unlocked) and kill the child
core_timeout = 3


def test_runner_wrapper(suite, keep_alive_pipe, result_pipe, failed_pipe):
    result = not VppTestRunner(
        keep_alive_pipe=keep_alive_pipe,
        failed_pipe=failed_pipe,
        verbosity=verbose,
        failfast=failfast).run(suite).wasSuccessful()
    result_pipe.send(result)
    result_pipe.close()
    keep_alive_pipe.close()
    failed_pipe.close()


class add_to_suite_callback:
    def __init__(self, suite):
        self.suite = suite

    def __call__(self, file_name, cls, method):
        suite.addTest(cls(method))


class Filter_by_class_list:
    def __init__(self, class_list):
        self.class_list = class_list

    def __call__(self, file_name, class_name, func_name):
        return class_name in self.class_list


def suite_from_failed(suite, failed):
    filter_cb = Filter_by_class_list(failed)
    return VppTestRunner.filter_tests(suite, filter_cb)


def run_forked(suite):
    keep_alive_parent_end, keep_alive_child_end = Pipe(duplex=False)
    result_parent_end, result_child_end = Pipe(duplex=False)
    failed_parent_end, failed_child_end = Pipe(duplex=False)

    child = Process(target=test_runner_wrapper,
                    args=(suite, keep_alive_child_end, result_child_end,
                          failed_child_end))
    child.start()
    last_test_temp_dir = None
    last_test_vpp_binary = None
    last_test = None
    result = None
    failed = set()
    last_heard = time.time()
    core_detected_at = None
    debug_core = os.getenv("DEBUG", "").lower() == "core"
    while True:
        readable = select.select([keep_alive_parent_end.fileno(),
                                  result_parent_end.fileno(),
                                  failed_parent_end.fileno(),
                                  ],
                                 [], [], 1)[0]
        if result_parent_end.fileno() in readable:
            result = result_parent_end.recv()
            break
        if keep_alive_parent_end.fileno() in readable:
            while keep_alive_parent_end.poll():
                last_test, last_test_vpp_binary,\
                    last_test_temp_dir, vpp_pid = keep_alive_parent_end.recv()
            last_heard = time.time()
        if failed_parent_end.fileno() in readable:
            while failed_parent_end.poll():
                failed_test = failed_parent_end.recv()
                failed.add(failed_test.__name__)
            last_heard = time.time()
        fail = False
        if last_heard + test_timeout < time.time() and \
                not os.path.isfile("%s/_core_handled" % last_test_temp_dir):
            fail = True
            global_logger.critical("Timeout while waiting for child test "
                                   "runner process (last test running was "
                                   "`%s' in `%s')!" %
                                   (last_test, last_test_temp_dir))
        elif not child.is_alive():
            fail = True
            global_logger.critical("Child python process unexpectedly died "
                                   "(last test running was `%s' in `%s')!" %
                                   (last_test, last_test_temp_dir))
        elif last_test_temp_dir and last_test_vpp_binary:
            core_path = "%s/core" % last_test_temp_dir
            if os.path.isfile(core_path):
                if core_detected_at is None:
                    core_detected_at = time.time()
                elif core_detected_at + core_timeout < time.time():
                    if not os.path.isfile(
                            "%s/_core_handled" % last_test_temp_dir):
                        global_logger.critical(
                            "Child python process unresponsive and core-file "
                            "exists in test temporary directory!")
                        fail = True

        if fail:
            failed_dir = os.getenv('VPP_TEST_FAILED_DIR')
            lttd = last_test_temp_dir.split("/")[-1]
            link_path = '%s%s-FAILED' % (failed_dir, lttd)
            global_logger.error("Creating a link to the failed " +
                                "test: %s -> %s" % (link_path, lttd))
            try:
                os.symlink(last_test_temp_dir, link_path)
            except Exception:
                pass
            api_post_mortem_path = "/tmp/api_post_mortem.%d" % vpp_pid
            if os.path.isfile(api_post_mortem_path):
                global_logger.error("Copying api_post_mortem.%d to %s" %
                                    (vpp_pid, last_test_temp_dir))
                shutil.copy2(api_post_mortem_path, last_test_temp_dir)
            if last_test_temp_dir and last_test_vpp_binary:
                core_path = "%s/core" % last_test_temp_dir
                if os.path.isfile(core_path):
                    global_logger.error("Core-file exists in test temporary "
                                        "directory: %s!" % core_path)
                    check_core_path(global_logger, core_path)
                    global_logger.debug("Running `file %s':" % core_path)
                    try:
                        info = check_output(["file", core_path])
                        global_logger.debug(info)
                    except CalledProcessError as e:
                        global_logger.error(
                            "Could not run `file' utility on core-file, "
                            "rc=%s" % e.returncode)
                        pass
                    if debug_core:
                        spawn_gdb(last_test_vpp_binary, core_path,
                                  global_logger)
            child.terminate()
            result = -1
            break
    keep_alive_parent_end.close()
    result_parent_end.close()
    failed_parent_end.close()
    return result, failed


if __name__ == '__main__':

    try:
        verbose = int(os.getenv("V", 0))
    except ValueError:
        verbose = 0

    default_test_timeout = 600  # 10 minutes
    try:
        test_timeout = int(os.getenv("TIMEOUT", default_test_timeout))
    except ValueError:
        test_timeout = default_test_timeout

    debug = os.getenv("DEBUG")

    s = os.getenv("STEP", "n")
    step = True if s.lower() in ("y", "yes", "1") else False

    parser = argparse.ArgumentParser(description="VPP unit tests")
    parser.add_argument("-f", "--failfast", action='count',
                        help="fast failure flag")
    parser.add_argument("-d", "--dir", action='append', type=str,
                        help="directory containing test files "
                             "(may be specified multiple times)")
    args = parser.parse_args()
    failfast = True if args.failfast == 1 else False

    suite = unittest.TestSuite()
    cb = add_to_suite_callback(suite)
    for d in args.dir:
        print("Adding tests from directory tree %s" % d)
        discover_tests(d, cb)

    try:
        retries = int(os.getenv("RETRIES", 0))
    except ValueError:
        retries = 0
    attempts = retries + 1
    if attempts > 1:
        print("Perform %s attempts to pass the suite..." % attempts)
    if (debug is not None and debug.lower() in ["gdb", "gdbserver"]) or step:
        # don't fork if requiring interactive terminal..
        sys.exit(not VppTestRunner(
            verbosity=verbose, failfast=failfast).run(suite).wasSuccessful())
    else:
        while True:
            result, failed = run_forked(suite)
            attempts = attempts - 1
            print("%s test(s) failed, %s attempt(s) left" %
                  (len(failed), attempts))
            if len(failed) > 0 and attempts > 0:
                suite = suite_from_failed(suite, failed)
                continue
            sys.exit(result)
