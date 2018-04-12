#!/usr/bin/env python
#
# Copyright (C) 2016 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""app_profiler.py: manage the process of profiling an android app.
    It downloads simpleperf on device, uses it to collect samples from
    user's app, and pulls perf.data and needed binaries on host.
"""

from __future__ import print_function
import argparse
import copy
import os
import os.path
import re
import shutil
import subprocess
import sys
import time

from binary_cache_builder import BinaryCacheBuilder
from simpleperf_report_lib import *
from utils import *

NATIVE_LIBS_DIR_ON_DEVICE = '/data/local/tmp/native_libs/'

class HostElfEntry(object):
    """ Represent a native lib on host in NativeLibDownloader. """
    def __init__(self, path, name, score):
        self.path = path
        self.name = name
        self.score = score

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return '[path: %s, name %s, score %s]' % (self.path, self.name, self.score)


class NativeLibDownloader(object):
    """ Download native libs on device.

    1. Collect info of all native libs in the native_lib_dir on host.
    2. Check the available native libs in /data/local/tmp/native_libs on device.
    3. Sync native libs on device.
    """
    def __init__(self, ndk_path, device_arch, adb):
        self.adb = adb
        self.readelf = ReadElf(ndk_path)
        self.need_archs = self._get_need_archs(device_arch)
        self.host_build_id_map = {}  # Map from build_id to HostElfEntry.
        self.device_build_id_map = {}  # Map from build_id to relative_path on device.
        self.name_count_map = {}  # Used to give a unique name for each library.
        self.dir_on_device = NATIVE_LIBS_DIR_ON_DEVICE
        self.build_id_list_file = 'build_id_list'

    def _get_need_archs(self, device_arch):
        """ Return the archs of binaries needed on device. """
        if device_arch == 'arm64':
            return ['arm', 'arm64']
        if device_arch == 'arm':
            return ['arm']
        if device_arch == 'x86_64':
            return ['x86', 'x86_64']
        if device_arch == 'x86':
            return ['x86']
        return []

    def collect_native_libs_on_host(self, native_lib_dir):
        self.host_build_id_map.clear()
        for root, _, files in os.walk(native_lib_dir):
            for name in files:
                if not name.endswith('.so'):
                    continue
                self.add_native_lib_on_host(os.path.join(root, name), name)

    def add_native_lib_on_host(self, path, name):
        build_id = self.readelf.get_build_id(path)
        if not build_id:
            return
        arch = self.readelf.get_arch(path)
        if arch not in self.need_archs:
            return
        sections = self.readelf.get_sections(path)
        score = 0
        if '.debug_info' in sections:
            score = 3
        elif '.gnu_debugdata' in sections:
            score = 2
        elif '.symtab' in sections:
            score = 1
        entry = self.host_build_id_map.get(build_id)
        if entry:
            if entry.score < score:
                entry.path = path
                entry.score = score
        else:
            repeat_count = self.name_count_map.get(name, 0)
            self.name_count_map[name] = repeat_count + 1
            unique_name = name if repeat_count == 0 else name + '_' + str(repeat_count)
            self.host_build_id_map[build_id] = HostElfEntry(path, unique_name, score)

    def collect_native_libs_on_device(self):
        self.device_build_id_map.clear()
        self.adb.check_run(['shell', 'mkdir', '-p', self.dir_on_device])
        if os.path.exists(self.build_id_list_file):
            os.remove(self.build_id_list_file)
        self.adb.run(['pull', self.dir_on_device + self.build_id_list_file])
        if os.path.exists(self.build_id_list_file):
            with open(self.build_id_list_file) as fh:
                for line in fh.readlines():
                    line = line.strip()
                    items = line.split('=')
                    if len(items) == 2:
                        self.device_build_id_map[items[0]] = items[1]

    def sync_natives_libs_on_device(self):
        # Push missing native libs on device.
        for build_id in self.host_build_id_map:
            if build_id not in self.device_build_id_map:
                entry = self.host_build_id_map[build_id]
                self.adb.check_run(['push', entry.path, self.dir_on_device + entry.name])
        # Remove native libs not exist on host.
        for build_id in self.device_build_id_map:
            if build_id not in self.host_build_id_map:
                name = self.device_build_id_map[build_id]
                self.adb.run(['shell', 'rm', self.dir_on_device + name])
        # Push new build_id_list on device.
        with open(self.build_id_list_file, 'w') as fh:
            for build_id in self.host_build_id_map:
                fh.write('%s=%s\n' % (build_id, self.host_build_id_map[build_id].name))
        self.adb.check_run(['push', self.build_id_list_file,
                            self.dir_on_device + self.build_id_list_file])
        os.remove(self.build_id_list_file)


class AppProfiler(object):
    """Used to manage the process of profiling an android app.

    There are three steps:
       1. Prepare profiling.
       2. Profile the app.
       3. Collect profiling data.
    """
    def __init__(self, config):
        self.check_config(config)
        self.config = config
        self.adb = AdbHelper(enable_switch_to_root=not config['disable_adb_root'])
        self.is_root_device = False
        self.android_version = 0
        self.device_arch = None
        self.app_arch = self.config['app_arch']
        self.app_program = self.config['app_package_name'] or self.config['native_program']
        self.app_pid = None
        self.record_subproc = None


    def check_config(self, config):
        config_names = ['app_package_name', 'native_program', 'cmd', 'native_lib_dir',
                        'apk_file_path', 'recompile_app', 'launch_activity', 'launch_inst_test',
                        'record_options', 'perf_data_path', 'profile_from_launch', 'app_arch',
                        'ndk_path']
        for name in config_names:
            if name not in config:
                log_exit('config [%s] is missing' % name)
        if config['app_package_name'] and config['native_program']:
            log_exit("We can't profile an Android app and a native program at the same time.")
        elif config['app_package_name'] and config['cmd']:
            log_exit("We can't profile an Android app and a cmd at the same time.")
        elif config['native_program'] and config['cmd']:
            log_exit("We can't profile a native program and a cmd at the same time.")
        elif not config['app_package_name'] and not config['native_program'] and not config["cmd"]:
            log_exit("Please set a profiling target: an Android app, a native program or a cmd.")
        if config['app_package_name']:
            if config['launch_activity'] and config['launch_inst_test']:
                log_exit("We can't launch an activity and a test at the same time.")
        native_lib_dir = config.get('native_lib_dir')
        if native_lib_dir and not os.path.isdir(native_lib_dir):
            log_exit('[native_lib_dir] "%s" is not a dir' % native_lib_dir)
        if config.get('download_libs') and not native_lib_dir:
            log_exit('-lib option should be set to download libraries on device.')
        apk_file_path = config.get('apk_file_path')
        if apk_file_path and not os.path.isfile(apk_file_path):
            log_exit('[apk_file_path] "%s" is not a file' % apk_file_path)
        if config['recompile_app']:
            if not config['launch_activity'] and not config['launch_inst_test']:
                # If recompile app, the app needs to be restarted to take effect.
                config['launch_activity'] = '.MainActivity'
        if config['profile_from_launch']:
            if not config['app_package_name']:
                log_exit('-p needs to be set to profile from launch.')
            if not config['launch_activity']:
                log_exit('-a needs to be set to profile from launch.')
            if not config['app_arch']:
                log_exit('--arch needs to be set to profile from launch.')


    def profile(self):
        log_info('prepare profiling')
        self.prepare_profiling()
        log_info('start profiling')
        self.start_and_wait_profiling()
        log_info('collect profiling data')
        self.collect_profiling_data()
        log_info('profiling is finished.')


    def prepare_profiling(self):
        self._get_device_environment()
        if self.config.get('download_libs'):
            self._download_native_libs()
        self._enable_profiling()
        self._recompile_app()
        self._restart_app()
        self._get_app_environment()
        if not self.config['profile_from_launch']:
            self._download_simpleperf()


    def _get_device_environment(self):
        self.is_root_device = self.adb.switch_to_root()
        self.android_version = self.adb.get_android_version()
        if self.android_version < 7:
            log_warning("app_profiler.py is not tested prior Android N, please switch to use cmdline interface.")
        self.device_arch = self.adb.get_device_arch()


    def _download_native_libs(self):
        downloader = NativeLibDownloader(self.config['ndk_path'], self.device_arch, self.adb)
        downloader.collect_native_libs_on_host(self.config['native_lib_dir'])
        downloader.collect_native_libs_on_device()
        downloader.sync_natives_libs_on_device()

    def _enable_profiling(self):
        self.adb.set_property('security.perf_harden', '0')
        if self.is_root_device:
            # We can enable kernel symbols
            self.adb.run(['shell', 'echo 0 >/proc/sys/kernel/kptr_restrict'])


    def _recompile_app(self):
        if not self.config['recompile_app']:
            return
        if self.android_version == 0:
            log_warning("Can't fully compile an app on android version < L.")
        elif self.android_version == 5 or self.android_version == 6:
            if not self.is_root_device:
                log_warning("Can't fully compile an app on android version < N on non-root devices.")
            elif not self.config['apk_file_path']:
                log_warning("apk file is needed to reinstall the app on android version < N.")
            else:
                flag = '-g' if self.android_version == 6 else '--include-debug-symbols'
                self.adb.set_property('dalvik.vm.dex2oat-flags', flag)
                self.adb.check_run(['install', '-r', self.config['apk_file_path']])
        elif self.android_version >= 7:
            self.adb.set_property('debug.generate-debug-info', 'true')
            self.adb.check_run(['shell', 'cmd', 'package', 'compile', '-f', '-m', 'speed',
                                self.config['app_package_name']])
        else:
            log_fatal('unreachable')


    def _restart_app(self):
        if not self.config['app_package_name']:
            return
        if not self.config['launch_activity'] and not self.config['launch_inst_test']:
            self.app_pid = self._find_app_process()
            if self.app_pid is not None:
                return
            else:
                self.config['launch_activity'] = '.MainActivity'

        self.adb.check_run(['shell', 'am', 'force-stop', self.config['app_package_name']])
        count = 0
        while True:
            time.sleep(1)
            pid = self._find_app_process()
            if pid is None:
                break
            # When testing on Android N, `am force-stop` sometimes can't kill
            # com.example.simpleperf.simpleperfexampleofkotlin. So use kill when this happens.
            count += 1
            if count >= 3:
                self.run_in_app_dir(['kill', '-9', str(pid)], check_result=False, log_output=False)

        if self.config['profile_from_launch']:
            self._download_simpleperf()
            self.start_profiling()

        if self.config['launch_activity']:
            activity = self.config['app_package_name'] + '/' + self.config['launch_activity']
            result = self.adb.run(['shell', 'am', 'start', '-n', activity])
            if not result:
                log_exit("Can't start activity %s" % activity)
        else:
            runner = self.config['app_package_name'] + '/android.support.test.runner.AndroidJUnitRunner'
            result = self.adb.run(['shell', 'am', 'instrument', '-e', 'class',
                                   self.config['launch_inst_test'], runner])
            if not result:
                log_exit("Can't start instrumentation test  %s" % self.config['launch_inst_test'])

        for i in range(10):
            self.app_pid = self._find_app_process()
            if self.app_pid is not None:
                return
            time.sleep(1)
            log_info('Wait for the app process for %d seconds' % (i + 1))
        log_exit("Can't find the app process")


    def _find_app_process(self):
        if not self.config['app_package_name'] and self.android_version >= 7:
            result, output = self.adb.run_and_return_output(['shell', 'pidof', self.app_program])
            return int(output) if result else None
        ps_args = ['ps', '-e', '-o', 'PID,NAME'] if self.android_version >= 8 else ['ps']
        result, output = self.adb.run_and_return_output(['shell'] + ps_args, log_output=False)
        if not result:
            return None
        for line in output.split('\n'):
            strs = line.split()
            if len(strs) < 2:
                continue
            process_name = strs[-1]
            if self.config['app_package_name']:
                # This is to match process names in multiprocess apps.
                process_name = process_name.split(':')[0]
            if process_name == self.app_program:
                pid = int(strs[0] if self.android_version >= 8 else strs[1])
                # If a debuggable app with wrap.sh runs on Android O, the app will be started with
                # logwrapper as below:
                # 1. Zygote forks a child process, rename it to package_name.
                # 2. The child process execute sh, which starts a child process running
                # /system/bin/logwrapper.
                # 3. logwrapper starts a child process running sh, which interprets wrap.sh.
                # 4. wrap.sh starts a child process running the app.
                # The problem here is we want to profile the process started in step 4, but
                # sometimes we run into the process started in step 1. To solve it, we can check
                # if the process has opened an apk file in some app dirs.
                if self.android_version >= 8 and self.config['app_package_name'] and (
                    not self._has_opened_apk_file(pid)):
                    continue
                return pid
        return None


    def _has_opened_apk_file(self, pid):
        result, output = self.run_in_app_dir(['ls -l /proc/%d/fd' % pid],
                                             check_result=False, log_output=False)
        return result and re.search(r'app.*\.apk', output)


    def _get_app_environment(self):
        if not self.config['cmd']:
            if self.app_pid is None:
                self.app_pid = self._find_app_process()
                if self.app_pid is None:
                    log_exit("can't find process for app [%s]" % self.app_program)
        if not self.app_arch:
            if not self.config['cmd'] and self.device_arch in ['arm64', 'x86_64']:
                output = self.run_in_app_dir(['cat', '/proc/%d/maps' % self.app_pid], log_output=False)
                if 'linker64' in output:
                    self.app_arch = self.device_arch
                else:
                    self.app_arch = 'arm' if self.device_arch == 'arm64' else 'x86'
            else:
                self.app_arch = self.device_arch
        log_info('app_arch: %s' % self.app_arch)


    def _download_simpleperf(self):
        simpleperf_binary = get_target_binary_path(self.app_arch, 'simpleperf')
        self.adb.check_run(['push', simpleperf_binary, '/data/local/tmp'])
        self.adb.check_run(['shell', 'chmod', 'a+x', '/data/local/tmp/simpleperf'])


    def start_and_wait_profiling(self):
        if self.record_subproc is None:
            self.start_profiling()
        self.wait_profiling()


    def wait_profiling(self):
        returncode = None
        try:
            returncode = self.record_subproc.wait()
        except KeyboardInterrupt:
            self.stop_profiling()
            self.record_subproc = None
            # Don't check return value of record_subproc. Because record_subproc also
            # receives Ctrl-C, and always returns non-zero.
            returncode = 0
        log_debug('profiling result [%s]' % (returncode == 0))
        if returncode != 0:
            log_exit('Failed to record profiling data.')


    def start_profiling(self):
        args = ['/data/local/tmp/simpleperf', 'record', self.config['record_options'],
                '-o', '/data/local/tmp/perf.data']
        if self.config['app_package_name']:
            args += ['--app', self.config['app_package_name']]
        elif self.config['native_program']:
            args += ['-p', str(self.app_pid)]
        elif self.config['cmd']:
            args.append(self.config['cmd'])
        if self.adb.run(['shell', 'ls', NATIVE_LIBS_DIR_ON_DEVICE]):
            args += ['--symfs', NATIVE_LIBS_DIR_ON_DEVICE]
        adb_args = [self.adb.adb_path, 'shell'] + args
        log_debug('run adb cmd: %s' % adb_args)
        self.record_subproc = subprocess.Popen(adb_args)


    def stop_profiling(self):
        """ Stop profiling by sending SIGINT to simpleperf, and wait until it exits
            to make sure perf.data is completely generated."""
        has_killed = False
        while True:
            (result, _) = self.adb.run_and_return_output(['shell', 'pidof', 'simpleperf'])
            if not result:
                break
            if not has_killed:
                has_killed = True
                self.adb.run_and_return_output(['shell', 'pkill', '-l', '2', 'simpleperf'])
            time.sleep(1)


    def collect_profiling_data(self):
        self.adb.check_run_and_return_output(['pull', '/data/local/tmp/perf.data',
                                              self.config['perf_data_path']])
        if self.config['collect_binaries']:
            config = copy.copy(self.config)
            config['binary_cache_dir'] = 'binary_cache'
            config['symfs_dirs'] = []
            if self.config['native_lib_dir']:
                config['symfs_dirs'].append(self.config['native_lib_dir'])
            binary_cache_builder = BinaryCacheBuilder(config)
            binary_cache_builder.build_binary_cache()


    def run_in_app_dir(self, args, stdout_file=None, check_result=True, log_output=True):
        args = self.get_run_in_app_dir_args(args)
        if check_result:
            return self.adb.check_run_and_return_output(args, stdout_file, log_output=log_output)
        return self.adb.run_and_return_output(args, stdout_file, log_output=log_output)


    def get_run_in_app_dir_args(self, args):
        if not self.config['app_package_name']:
            return ['shell'] + args
        if self.is_root_device:
            return ['shell', 'cd /data/data/' + self.config['app_package_name'] + ' && ' +
                      (' '.join(args))]
        return ['shell', 'run-as', self.config['app_package_name']] + args

def main():
    parser = argparse.ArgumentParser(
        description=
"""Profile an Android app or native program.""")
    parser.add_argument('-p', '--app', help=
"""Profile an Android app, given the package name. Like -p com.example.android.myapp.""")
    parser.add_argument('-np', '--native_program', help=
"""Profile a native program. The program should be running on the device.
Like -np surfaceflinger.""")
    parser.add_argument('-cmd', help=
"""Run a cmd and profile it. Like -cmd "pm -l".""")
    parser.add_argument('-lib', '--native_lib_dir', help=
"""Path to find debug version of native shared libraries used in the app.""")
    parser.add_argument('--download_libs', action='store_true', help= """Download native
libraries in native_lib_dir on device.""")
    parser.add_argument('-nc', '--skip_recompile', action='store_true', help=
"""When profiling an Android app, by default we recompile java bytecode to native instructions
to profile java code. It takes some time. You can skip it if the code has been compiled or you
don't need to profile java code.""")
    parser.add_argument('--apk', help=
"""When profiling an Android app, we need the apk file to recompile the app on
Android version <= M.""")
    parser.add_argument('-a', '--activity', help=
"""When profiling an Android app, start an activity before profiling.
It restarts the app if the app is already running.""")
    parser.add_argument('-t', '--test', help=
"""When profiling an Android app, start an instrumentation test before profiling.
It restarts the app if the app is already running.""")
    parser.add_argument('--arch', help=
"""Select which arch the app is running on, possible values are:
arm, arm64, x86, x86_64. If not set, the script will try to detect it.""")
    parser.add_argument('-r', '--record_options',
                        default='-e task-clock:u -g -f 1000 --duration 10', help="""
                        Set options for `simpleperf record` command.
                        Default is "-e task-clock:u -g -f 1000 --duration 10".""")
    parser.add_argument('-o', '--perf_data_path', default="perf.data", help=
"""The path to store profiling data.""")
    parser.add_argument('-nb', '--skip_collect_binaries', action='store_true', help=
"""By default we collect binaries used in profiling data from device to
binary_cache directory. It can be used to annotate source code. This option skips it.""")
    parser.add_argument('--profile_from_launch', action='store_true', help=
"""Profile an activity from initial launch. It should be used with -p, -a, and --arch options.
Normally we run in the following order: restart the app, detect the architecture of the app,
download simpleperf and native libs with debug info on device, and start simpleperf record.
But with --profile_from_launch option, we change the order as below: kill the app if it is
already running, download simpleperf on device, start simpleperf record, and start the app.""")
    parser.add_argument('--disable_adb_root', action='store_true', help=
"""Force adb to run in non root mode.""")
    parser.add_argument('--ndk_path', nargs=1, help='Find tools in the ndk path.')
    args = parser.parse_args()
    config = {}
    config['app_package_name'] = args.app
    config['native_program'] = args.native_program
    config['cmd'] = args.cmd
    config['native_lib_dir'] = args.native_lib_dir
    config['download_libs'] = args.download_libs
    config['recompile_app'] = args.app and not args.skip_recompile
    config['apk_file_path'] = args.apk

    config['launch_activity'] = args.activity
    config['launch_inst_test'] = args.test

    config['app_arch'] = args.arch
    config['record_options'] = args.record_options
    config['perf_data_path'] = args.perf_data_path
    config['collect_binaries'] = not args.skip_collect_binaries
    config['profile_from_launch'] = args.profile_from_launch
    config['disable_adb_root'] = args.disable_adb_root
    config['ndk_path'] = None if not args.ndk_path else args.ndk_path[0]

    profiler = AppProfiler(config)
    profiler.profile()

if __name__ == '__main__':
    main()
