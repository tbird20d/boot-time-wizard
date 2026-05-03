#!/usr/bin/python3
# SPDX-License-Identifier: MIT
# vim: set ts=4 sw=4 et :
#
# boot-time-tuning-wizard.py
#   Run a series of measurements and tests, to try to optimize boot-time
#     for a platform
#
# todo:
# - hide stderr from builds, if requested (default opt-in to build messages)
#   - or show if requested (default opt-out to build messages)
# - detect build failures
# - handle different optimization methods:
#   - change command line setting
#     - add low_mem
#     - add deferred initcalls
#   - remove devicetree entries
#   - remove udev entries
#   - remove SELinux rules
# - during test, check that system still behaves properly
#   - do a system sanity check
#   - allow specifying a validation function or program
# - try multiple methods
#   - undo each method before trying the next one
#   - allow stacking of methods
#     - do not undo each method before trying the next one
#   - select method(s) to use
#     - find element(s) to change (something over 10 ms)
#
# overall flow:
#  for a given item:
#    baseline: instrument, build, boot, measure, check functionality
#    test: alter, build, boot, measure, check functionality
#    analyze: compare results, make recommendations
#    recover: de-instrument, restore (build, boot, check)
#
# Notes:
# - use instrumentation to determine if a kernel feature is used or not,
#    and turn it off in kernel config, dt, command line, systemd, or in source
# - instrumentation can use /proc, /sys, ftrace, inserted printks
#   - printks could be programmatically inserted (maybe by AI?)
# - need to validate that nothing breaks with a comprehensive workload and
#   test cycle
# - examples:
#   - disable or remove all DT devices that are not loaded during a normal boot
# - need classes with methods for:
#   - instrument, build, boot, measure, check-functionality, alter,
#   - compare, restore
# - need lists of items to try: bluetooth, sound, video, serial console,
#   - wifi, disable polkit, disable ptrace, disable users,
#   - trim filesystems, trim code, remove unused dma items, remove drivers,
#   - remove unused crypto algorithms, remove unused syscalls
#   - remove unused device nodes, remove unused keymaps, disable printks
#   - remove kernel functions (directly, not via config)
# - need automated recovery in case of over-adjustment
#   - platform needs to support direct automated kinstall, or
#   - platform needs automatic reversion to recovery kernel
#     (e.g. support for boot-once)
# - may need mappings:
#   - code -> config
#   - instrumentation -> code location (e.g. printk -> config)
#   - procfs info -> driver/feature -> code -> config or DT node
#   - sysfs info -> driver/feature -> config or DT node
#

VERSION=(0,5,0)

import sys
import os
import re
import shutil
from signal import signal, SIGPIPE, SIG_DFL
from subprocess import getstatusoutput
import subprocess
import random

debug = False
verbose = False

DEFAULT_GBD_SCRIPT_PATH = "/usr/local/bin"
ALTERNATE_GBD_SCRIPT_PATH = "/home/tbird/bin"
gbd_script_path = None

# use run_prefix to distinguish files in the artifact directory
run_prefix = "run-000000-"
# put this on test kernels to distinguish them
kernel_id = "999999"

def eprint(msg):
    sys.stderr.write("Error: " + msg + '\n')
    sys.stderr.flush()

def error_out(msg, rcode=-1):
    eprint(msg)
    sys.exit(rcode)

def wprint(msg):
    sys.stderr.write("Warning: " + msg + '\n')
    sys.stderr.flush()

def dprint(msg):
    global debug

    if debug:
        print("DEBUG: " + str(msg))

def vprint(msg):
    global verbose

    if verbose:
        print(msg)

# put a wrapper around subprocess execution
# if in verbose mode,
#   output to my stdout while the subprocess is running
#   and return the empty string as output
# else use getstatusoutput to capture (and hide) the subprocess output.
# returns (status, output)
def execute(cmd, verbose=False):
    if verbose:
        try:
            result = subprocess.run(cmd, shell=True)
            status = result.returncode
            output = ""
        except subprocess.CalledProcessError as cp:
            status = cp.returncode
            output = cp.output

        return (status, output)
    else:
        return getstatusoutput(cmd)

# parse kernel config into a dictionary
# returns a dictionary of (long) config names and their values
def parse_kernel_config(config_text):
    CONFIGS = {}
    for line in config_text.split('\n'):
        line = line.strip()
        # skip blank and comment lines
        if not line:
            continue
        if line.startswith("#") and not line.endswith(" is not set"):
            continue
        if line.startswith("CONFIG_"):
            name, value = line.split("=", 1)
            CONFIGS[name] = value
        elif line.endswith(" is not set"):
            # strip leading '# '
            name = line[2:].split(" ")[0]
            CONFIGS[name] = "n"
        else:
            eprint("weirdness in config file at line: '%s'" % line)
    return CONFIGS

# define a class for optimization methods
class opt_method_class():
    def __init__(self, name, description, opt_type="unknown"):
        self.name = name
        self.description = description
        self.opt_type = opt_type
        self.results_txt = "#"*70  + "\n" + \
            "=== Results report for optimization method '%s' ===\n" % name
        self.compare_list = ["time_to_init"]

        # artifact_prefix gets set later
        # set to something that will raise exception if used in string context
        self.artifact_prefix = 999

    def save_baseline(self, arg):
        raise NotImplementedError

    def restore_baseline(self, arg):
        raise NotImplementedError

    def instrument_target(self, target):
        # FIXTHIS - could put general instrumentation, like
        # adding 'log_buf_len=10M quiet initcall' to cmdline
        print("No additional instrumentation needed")

    def detect_optimization(self, arg):
        # should return (TRUE|FALSE, msg)
        return NotImplementedError

    def apply_optimization(self, arg, kernel_id=None):
        raise NotImplementedError

    def set_results(self, results_txt):
        self.results_txt += results_txt
        self.results_txt += "\n##########\n"

    def show_results(self):
        print(self.results_txt)

    def get_compare_list(self):
        # return method-specific items to compare in a report
        return self.compare_list

# the optimization method class for config items
class config_om_class(opt_method_class):
    def __init__(self, name, description, config_name, config_value):
        super().__init__(name, description, "kernel_config")
        self.config_name = config_name
        self.config_value = config_value
        #self.kernel_cmdline_path = ""

    def save_baseline(self, target):
        target.save_build_config()
        return

    def restore_baseline(self, target):
        target.restore_build_config()

        print(" - Rebuilding kernel...")
        status, output = target.ttc("kbuild", verbose)

        print(" - Installing kernel...")
        status, output = target.ttc("kinstall", verbose)

        # reset working dir to where we started
        target.change_to_start_dir()

    def detect_optimization(self, target):
        # report whether this optimization is already applied
        # check config, return a string
        configs = target.get_target_configs()

        if self.config_name not in configs:
            return (False, "config %s not found")

        if configs[self.config_name] == self.config_value:
            return (True, "config %s has the value %s" % self.config_name, self.config_value)
        else:
            return (False, "config %s: current value of %s does not match desired value of %s" % (self.config_name, configs[self.config_name], self.config_value))

    def apply_optimization(self, target, kernel_id):
        target.change_to_src_dir()

        # check if config is different from requested value
        configs = target.get_target_configs()

        old_value = configs.get(self.config_name, "missing")
        new_value = self.config_value

        old_build_value = target.get_build_config(self.config_name)

        dprint("config: old_build_value=%s, old_value=%s, new_value=%s" % (old_build_value, old_value, new_value))
        # compare config in source with config
        if old_value == new_value:
            wprint("in apply_optimization, old config value matches requested config value")
            wprint(" old_value=%s, new_value=%s, old_build_value=%s" % (old_value, new_value, old_build_value))

        # set config to new value
        print(" - Applying optimization: changing config value %s: %s -> %s" % (self.config_name, old_build_value, new_value))
        target.set_build_config(self.config_name, new_value)

        # TRB: wait for input (to investigate what happened)
        #input("Press enter to continue...")

        self.saved_value = old_build_value

        # set kernel id
        target.set_localversion(kernel_id)

        # rebuild kernel
        print(" - Rebuilding kernel...")
        status, output = target.ttc("kbuild", verbose)

        # install kernel
        print(" - Installing kernel...")
        status, output = target.ttc("kinstall", verbose)

        # reset working dir to where we started
        target.change_to_start_dir()

        return status, output

    def undo_optimization(self, target):
        target.change_to_src_dir()

        # set config to saved value
        #target.set_build_config(self.config_name, self.saved_value)
        #print(" - Undoing optimization: changing config value %s=%s", self.config_name, self.saved_value)

        # revert to saved build config
        target.restore_build_config()

        # clear kernel id
        target.clear_localversion()

        # rebuild kernel
        print(" - Rebuilding kernel...")
        status, output = target.ttc("kbuild", verbose)

        # install kernel
        print(" - Installing kernel...")
        status, output = target.ttc("kinstall", verbose)

        # reset working dir to where we started
        target.change_to_start_dir()
        return

# the optimization method class for kernel command line options
class cmdline_om_class(opt_method_class):
    def __init__(self, name, description, option_str):
        super().__init__(name, description, "kernel_cmdline")
        self.option_str = option_str
        if "=" in option_str:
            self.option_name, self.option_value = option_str.split("=", 1)


    def save_baseline(self, target):
        # save the current (configured) target cmdline
        status, cur_cmdline = target.ttc("get_cmdline")
        saved_cmdline_path = self.artifact_prefix + "saved_cmdline.txt"
        with open(saved_cmdline_path, "w") as fd:
                  fd.write(cur_cmdline)
        return

    def restore_baseline(self, target):
        saved_cmdline_path = self.artifact_prefix + "saved_cmdline.txt"
        cmdline = open(saved_cmdline_path, "r").read()
        status, output = target.ttc("set_cmdline -r " + cmdline)

    def detect_optimization(self, target):
        # report whether this optimization is already applied
        # check config, return a string
        status, cmdline = target.ttc("get_cmdline -s")

        if self.option_str in cmdline:
            return (True, "cmdline option '%s' is already in current command line" % self.option_str)

        return (False, "Did not find '%s' in current command line" % self.option_str)

    def apply_optimization(self, target, kernel_id):
        print(" - Applying optimization: adding '%s' to command line" % self.option_str)

        status, cmdline = target.ttc("get_cmdline")
        if status != 0:
             eprint("Could not get current configured command line")

        self.saved_cmdline = cmdline

        # FIXTHIS - for now, assume option can be appended at the end
        target.ttc("set_cmdline " + self.option_str)
        return status

    def undo_optimization(self, target):
        status, cmdline = target.ttc("set_cmdline -r" + self.saved_cmdline)
        if status != 0:
             eprint("Could not set current configured command line")
        return

class target_class():
    def __init__(self, name):
        # save start dir for restoration later
        self.start_dir = os.getcwd();

        # sets up target instance
        # raises ValueError on invalid target name
        self.name = name
        # make sure target is recognized by ttc
        status, output = self.ttc("info")
        if status != 0:
            raise ValueError

        # set some variables we may need later
        self.KERNEL_SRC = self.get_ttc_var("KERNEL_SRC", ".")
        if not os.path.isfile(self.KERNEL_SRC + "/MAINTAINERS"):
            wprint("Missing MAINTAINERS file in KERNEL_SRC dir: %s - check configuration" % self.KERNEL_SRC)
            dprint("current dir=%s" % os.getcwd())
        # make KERNEL_SRC path absolute
        self.KERNEL_SRC = os.path.abspath(self.KERNEL_SRC)

        self.KBUILD_OUTPUT = self.get_ttc_var("KBUILD_OUTPUT", self.KERNEL_SRC)

        # make this path absolute
        self.change_to_src_dir()
        self.KBUILD_OUTPUT = os.path.abspath(self.KBUILD_OUTPUT)
        self.change_to_start_dir()

        self.CONFIGS = {}   # fill the CONFIGS dictionary in, as needed
        self.config_saved = False

        self.artifact_prefix = 999

    # return the output from 'ttc target cmd'
    def ttc(self, cmd, echo=False):
        cmdline = "ttc %s %s" % (self.name, cmd)
        status, output = execute(cmdline, echo)
        if echo:
            print(output)
        dprint("cmdline='%s'" % cmdline)
        dprint("output='%s'" % output)
        return (status, output)

    def get_ttc_var(self, var, default):
        status, output = execute("ttc %s info -n %s" % (self.name, var))
        if status == 0:
            return output.strip()
        else:
            return default

    def run(self, cmd, echo=False):
        return self.ttc("run " + cmd, echo)

    def put(self, src_path, dest_path):
        return self.ttc("cp %s target:%s" % (src_path, dest_path))

    def get(self, src_path, dest_path):
        return self.ttc("cp target:%s %s" % (src_path, dest_path))

    def reboot_and_wait(self, echo=True):
        status, output = self.ttc("reboot", echo)
        status2, output2 = self.ttc("wait_for -t 30 ttc run true", echo)
        return status2, output + output2

    def get_target_config(self):
        # returns the kernel configuration from a target as a string

        # look on the target in a variety of locations for the kernel config
        # if found, return the kernel config
        # if not, return the empty string
        status, output = self.run("zcat /proc/config.gz")
        if status == 0:
            return output

        status, output = self.run("uname -r")
        if status == 0:
            release = output.strip()

        # if there's a config.ko module, try installing it
        config_ko_path = "/lib/modules/%s/kernel/kernel/configs.ko" % release
        status, output = self.run("test -f %s" % config_ko_path)
        if status == 0:
            self.run("insmod %s" % config_ko_path)
            status, output = self.run("zcat /proc/config.gz")
            self.run("rmmod configs")
            if status == 0:
                return output

        # if there's a config.ko.xz module, try installing it
        config_koxz_path = "/lib/modules/%s/kernel/kernel/configs.ko.xz" % release
        status, output = self.run("test -f %s" % config_koxz_path)
        if status == 0:
            self.run("insmod %s" % config_koxz_path)
            status, output = self.run("zcat /proc/config.gz")
            self.run("rmmod configs")
            if status == 0:
                return output

        # if there's a build .config file, return that
        build_config_path = "/lib/modules/%s/build/.config" % release
        status, output = self.run("test -f %s" % build_config_path)
        if status == 0:
            status, output = self.run("cat %s" % build_config_path)
            if status == 0:
                return output

        # check /boot directory
        boot_config_path = "/boot/config-%s" % release
        status, output = self.run("test -f %s" % boot_config_path)
        if status == 0:
            status, output = self.run("cat %s" % boot_config_path)
            if status == 0:
                return output

        boot_config_path = "/boot/config"
        status, output = self.run("test -f %s" % boot_config_path)
        if status == 0:
            status, output = self.run("cat %s" % boot_config_path)
            if status == 0:
                return output

        return ""

    def get_target_configs(self):
        # returns the kernel configuration from a target as a dictionary
        # config names are long, including CONFIG_ prefix
        # (e.g. 'CONFIG_CMDLINE')
        if self.CONFIGS:
            return self.CONFIGS

        config_text = self.get_target_config()
        if config_text:
            CONFIGS = parse_kernel_config(config_text)
            self.CONFIGS = CONFIGS

        return self.CONFIGS

    def change_to_src_dir(self):
        os.chdir(self.KERNEL_SRC)

    def change_to_start_dir(self):
        os.chdir(self.start_dir)

    def get_build_config(self, config_name):
        # returns the value of a single config option in the current
        # build config

        # read build configs file ($KBUILD_OUTPUT/.config)
        config_path = self.KBUILD_OUTPUT + "/.config"
        value = "undefined"
        with open(config_path, "r") as fd:
            for line in fd.readlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("%s=" % config_name):
                    value = line.split("=")[1].strip()
                    break
                if line.startswith("# %s=" % config_name) and \
                    line.endswith(" is not set"):
                    value = "n"
                    break

        return value

    def save_build_config(self, artifact_prefix):
        if not self.config_saved:
            # put original build configs into artifact dir
            config_path = self.KBUILD_OUTPUT + "/.config"
            save_path = self.artifact_prefix + "saved-config"
            dprint("Saving build config: from %s to %s" % (config_path, save_path))
            shutil.copy(config_path, save_path)
            self.config_saved = True

    def restore_build_config(self, artifact_prefix):
        if not self.config_saved:
            eprint("Error restoring build config - original config was not saved!")
            return

        # copy original build configs back into build dir
        save_path = self.artifact_prefix + "saved-config"
        config_path = self.KBUILD_OUTPUT + "/.config"
        dprint("Restoring build config: from %s to %s" % (save_path, config_path))
        shutil.copy(save_path, config_path)

    def set_build_config(self, config_name, config_value):
        global verbose

        cmd = 'set_config "%s=%s"' % (config_name, config_value)
        # FIXTHIS - need to test set_build_config with quoted string values

        return self.ttc(cmd, verbose)

    def set_localversion(self, lver_str):
        # we should be in the KERNEL_SRC dir by now
        lv_path = "localversion"
        lv_file = open(lv_path, "w")
        lv_file.write("-" + lver_str + "\n")
        lv_file.close()
        self.kernel_id = lver_str

    def clear_localversion(self):
        # we should be in the KERNEL_SRC dir by now
        lv_path = "localversion"
        try:
            os.unlink(lv_path)
        except:
            pass

    def check_localversion(self, lver_str = None):
        if not lver_str:
            lver_str = self.kernel_id
        rcode, output = self.run("uname -r")
        if lver_str in output:
            return True
        else:
            return False

def usage():
    print("""Usage: boot-time-tuning-wizard.py <options>

Run one or more optimization methods on a specified target, and
see how it affects the boot-time for the target.  Produce a report
showing the effect of each method and the cumulative effect of
all applied methods.

Options:
 -h, --help    Show this usage help
 -m <method>   Test specified optimization method
 -l            List optimization methods
 -t <target>   Specify the target to run tests on
 -d <dir>      Use <dir> for artifacts and test results
               (default is the './bttw-artifacts')
 -k <kernel_dir> Use <kernel_dir> as the kernel source directory.
               This is required by some optimization methods.  If not
               specified, the code will see if the current directory or
               'linux' is a kernel source dir.  If a kernel source dir
               cannot be found, optimizations that require it will be
               skipped.
 -v            Show verbose output.  When used with -l and -t, shows
               the status of each optimization method on the given target.
 --debug       Show debug information
 -r <run_id>   Generate a report from boot-data files from the indicated
               run_id.  Other parameters, such as -m, -t and -k must be
               the same as the original run.  The program must be run from
               the same directory as it was originally. This is primarily
               used to test the report generator, on a single method.
""")
    sys.exit(0)

# returns a list of method instances
def init_methods(kernel_dir):
    # add methods
    methods = {}

    cur_dir = os.getcwd()

    # initialize optimization methods that require modifying or
    # reconfiguring the kernel
    if kernel_dir:
        m = config_om_class("disable_bluetooth", "Disable bluetooth by compiling kernel without it.", "CONFIG_BT", "N")
        m.compare_list = ["CONFIG_BT", "bt_init", "bluetooth.service"]
        methods[m.name] = m

        m = config_om_class("disable_sound", "Disable sound by compiling kernel without it.", "CONFIG_SOUND", "N")
        m.compare_list = ["CONFIG_SOUND", "alsa_pcm_init", "alsa_sound_init", "alsa_timer_init", "init_soundcore", "systemd-modules-load.service"]
        methods[m.name] = m

        m = config_om_class("disable_graphics", "Disable graphics by compiling kernel without it.", "CONFIG_DRM", "N")
        m.compare_list = ["CONFIG_DRM"]
        methods[m.name] = m

        m = cmdline_om_class("low_mem", "Reduce memory configure (to 500M) using cmdline option", "mem=500M")
        m.compare_list = ["MEM_TOTAL", "MEM_USED", "mm"]
        methods[m.name] = m

    dprint("== Methods ==")
    dprint(methods)

    # FIXTHIS - add more methods here

    # candidate methods:
    #   optimize udev (remove unused udev rules)
    #   optimize filesystems (eliminate unused filesystems)
    #   optimize kernel size
    #   use 'quiet'
    #   use deferred initcalls
    #   use deferred memory init
    #   disable printing
    #   optimize device tree (remove unused dt nodes)
    #   optimize security init (remove unused selinux rules)
    #   use async probing
    #   use async module loading

    return methods

def list_methods(methods, target):
    global verbose

    print("Available optimization methods:")
    for m in methods.values():
        print("  " + m.name, end="")
        if verbose and target:
            # show the status of optimization methods for the indicated target
            has_method, msg = m.detect_optimization(target)
            if has_method:
                print(", present (%s)" % msg)
            else:
                print(", NOT applied (%s)" % msg)
        else:
            print()

    if not (verbose and target):
        print("\nNote: use -v and specify a target to see current status")

def try_one_method(method, target, artifact_prefix):
    global kernel_id
    global gbd_script_path

    print("##################################################")
    print("== Trying optimization method: %s" % method.name)

    print("== Preparing target: %s" % target.name)
    vprint(" - Installing grab-boot-data.sh...")
    # find suitable destination directory
    #  see if /usr/local/bin exists on target
    dest_dir = "/usr/local/bin"
    rcode, output = target.run("ls " + dest_dir)
    if "No such" in output:
        dest_dir = "/usr/bin"
        rcode, output = target.run("ls " + dest_dir)
        if "No such" in output:
            dest_dir = "/bin"

    # FIXTHIS - should record dest_dir for later cleanup
    target.put(gbd_script_path, dest_dir)

    print("== Instrumenting target: %s, for method %s" % (target.name, method.name))
    method.instrument_target(target)

    vprint(" - Gathering baseline boot data...")
    target.run("chmod a+x %s/grab-boot-data.sh" % dest_dir)

    print("== Measuring baseline for target: %s" % target.name)
    # measure metric we are trying to optimize
    # measure full boot time
    # get a baseline measurement
    vprint(" - Rebooting %s..." % target.name)
    status, output = target.reboot_and_wait()
    if status != 0:
        error_out("Problem rebooting %s" % target.name)

    status, output = target.run("/usr/local/bin/grab-boot-data.sh -l timslab -m %s -d /tmp -x" % (target.name))
    status, output = target.run("ls -t /tmp/boot-data* | tail -n 1")
    baseline_results_path=output.strip()
    baseline_file = "baseline-" + os.path.basename(baseline_results_path)
    dprint("boot-data baseline file for this run on the target is: %s" % baseline_results_path)

    # download and save boot data file
    dprint("#######################################################")
    vprint("== Retrieving baseline boot data from %s..." % baseline_results_path)
    local_baseline_path = artifact_prefix + baseline_file
    target.get(baseline_results_path, local_baseline_path)

    # apply method
    #  - determine new value for option
    #  - build, reboot, alter commands/parameters, etc.
    dprint("#######################################################")
    vprint("== Applying optimization %s" % method.name)
    method.apply_optimization(target, kernel_id)

    # boot machine
    dprint("#######################################################")
    vprint("== Rebooting machine (with test kernel)")
    status, output = target.reboot_and_wait()
    vprint(output)

    # measure metric we are trying to optimize
    # measure full boot time
    dprint("#######################################################")
    vprint("== Measuring with optimization")
    vprint(" - Gathering test results boot data...")
    status, output = target.run("/usr/local/bin/grab-boot-data.sh -l timslab -m %s -d /tmp -x" % (target.name))
    # download and save boot data file
    status, output = target.run("ls -t /tmp/boot-data* | tail -n 1")
    results_path=output.strip()
    dprint("boot-data file for this run on the target is: %s" % results_path)
    results_file="results-"+ os.path.basename(results_path)

    # download and save boot data file
    vprint(" - Retrieving test results boot data from %s..." % results_file)
    local_results_path = artifact_prefix + results_file
    target.get(results_path, local_results_path)

    dprint("#######################################################")
    vprint("== Comparing baseline with optimization results")

    # generate report
    report_data = gen_report_data(method, local_baseline_path, local_results_path)

    method.set_results(report_data)

    print("== Done with method %s" % method.name)
    return output

def tune_boot_time(methods, target, artifact_prefix):
    global kernel_id

    rnd_str = artifact_prefix[-7:-1]

    print("Start of boot-time tuning:")

    # run through all listed methods
    mlist = list(methods.keys())
    for m in mlist:
        # FIXTHIS - advance run_count
        run_count = int(kernel_id.split("-")[-1]) + 1
        kernel_id = "%s-%d" % (rnd_str, run_count)

        # FIXTHIS - select a method
        # try it
        m.apply_optimization(target, kernel_id)

        # reset and try again
        dprint("#######################################################")
        dprint("== Undo Optimization ==")
        m.undo_optimization(target)

        # FIXTHIS - decide if optimization was worth it

    print("Final tuning recommendations:...")

def report_header(run_id, tname, saved_args):
    global VERSION

    arg_str = " ".join(saved_args)

    from datetime import datetime

    now = datetime.now()
    time_str = now.strftime("%Y-%02m-%02d_%02H:%02M:%02S")
    header = """Boot-Time Tuning Wizard Report
==========================================
DATE_AND_TIME="%s"
RUN_ID="%s"
TARGET_NAME="%s"
BTTW_ARGS="%s"
BTTW_VERSION="%s.%s.%s"
==========================================
""" % (time_str, run_id, tname, arg_str, VERSION[0], VERSION[1], VERSION[2])

    return header

def log_to_report(artifact_prefix, msg, append=True):
    report_path = artifact_prefix + "report.txt"
    if append:
        mode = "a"
    else:
        mode = "w"

    with open(report_path, mode) as report_file:
        report_file.write(msg)

def get_opt_with_arg(arg):
    try:
        arg_pos = sys.argv.index(arg)
        var = sys.argv[arg_pos + 1]
        del sys.argv[arg_pos + 1]
        del sys.argv[arg_pos]
    except IndexError:
        error_out("Missing argument to '%s'. Use -h for help" % arg)

    return var

def get_boolean_opt(arg):
    if arg in sys.argv:
        sys.argv.remove(arg)
        return True
    else:
        return False

# set global gbd_script_path, where grab-boot-data.sh can
# be found, to use on the target
def find_gbd_script_path(script_dir):
    global gbd_script_path

    # try some different alternatives
    script_path = script_dir + "/grab-boot-data.sh"
    if os.path.isfile(script_path) and os.access(script_path, os.X_OK):
        gbd_script_path = script_path
        return

    script_path = DEFAULT_GBD_SCRIPT_PATH + "/grab-boot-data.sh"
    if os.path.isfile(script_path) and os.access(script_path, os.X_OK):
        gbd_script_path = script_path
        return

    script_path = ALTERNATE_GBD_SCRIPT_PATH + "/grab-boot-data.sh"
    if os.path.isfile(script_path) and os.access(script_path, os.X_OK):
        gbd_script_path = script_path
        return

    error_out("Can't find grab-boot-data.sh on system!")

def gen_report_data(method, baseline_path, results_path):
    report_data = "#"*70  + "\n" + \
        "=== Results report for optimization method '%s' ===\n" % method.name

    # compare a few summary items:
    base_compare_list = ["time_to_init", "SYSTEMD_KERNEL_USECS", "SYSTEMD_USERSPACE_USECS"]

    compare_list = base_compare_list + method.get_compare_list()

    # Then compare some method-specific items:
    header = "-- Individual item comparisons --\n"
    # make a list of items to compare
    compare_str = " ".join(compare_list)

    cmd = "boot-data compare %s -f %s %s" % (baseline_path, results_path,
                                          compare_str)
    status, output = getstatusoutput(cmd)

    report_data += header + output + "\n\n"

    header = "-- Boot-data diff of baseline and results --\n"
    cmd = "boot-data diff %s -f %s" % (baseline_path, results_path)
    status, output = getstatusoutput(cmd)
    report_data += header + output + "\n\n"

    # FIXTHIS - raw diff is crude, let's omit for now
    #header = "-- Raw diff of baseline and results --\n"
    #cmd = "diff -u %s %s" % (baseline_path, results_path)
    #status, output = getstatusoutput(cmd)
    #report_data += header + output

    return report_data

# generate a report from existing run artifacts
def generate_report(report_run_id, method, target, artifact_dir, saved_args):
    global run_prefix

    if report_run_id.startswith("run-"):
        report_run_id = report_run_id[4:]

    if len(report_run_id) != 6:
        error_out("Invalid run-id.  It must be a 6-digit number.")

    run_prefix = "run-%s-" % report_run_id
    artifact_prefix = artifact_dir + "/" + run_prefix

    report_path = artifact_prefix + "report.txt"

    if os.path.exists(report_path):
        print("Report '%s' already exists" % report_path)
        response = input("OK to overwrite? [Y/n] ")
        if not response == "Y":
            print("Report Generation cancelled!")
            sys.exit(0)

    # find file paths
    candidates = os.listdir(artifact_dir)
    baseline_prefix = run_prefix + "baseline"
    results_prefix = run_prefix + "results"
    baseline_path = ""
    results_path = ""
    for candidate in candidates:
        if candidate.startswith(baseline_prefix):
            baseline_path = artifact_dir + "/" + candidate
        if candidate.startswith(results_prefix):
            results_path = artifact_dir + "/" + candidate

    if not baseline_path:
        error_out("Cannot find baseline boot-data file. Possible invalid run-id '%s'" % report_run_id)
    if not results_path:
        error_out("Cannot find results boot-data file. Possible invalid run-id '%s'" % report_run_id)

    # clean up saved_args - remove '-r <run-id>'
    arg_pos = saved_args.index("-r")
    del saved_args[arg_pos + 1]
    del saved_args[arg_pos]

    rheader = report_header(run_prefix[:-1], target.name, saved_args)
    report_data = gen_report_data(method, baseline_path, results_path)

    # False = do not append
    log_to_report(artifact_prefix, rheader + report_data, False)

    print("Report data is in: '%s'" % report_path)
    sys.exit(0)


def main():
    global debug
    global verbose
    global run_prefix, kernel_id

    saved_args = sys.argv[1:]

    script_dir = os.path.dirname(sys.argv[0])

    # allow error-free piping to other command line utilities (like 'head')
    signal(SIGPIPE, SIG_DFL)

    # parse command line arguments
    target = None
    method = None
    method_name = None
    artifact_dir = ""
    artifact_dir_created = False
    kernel_dir = ""
    do_list = False
    do_gen_report = False

    if "-h" in sys.argv or "--help" in sys.argv:
        usage()

    verbose = get_boolean_opt('-v')
    debug = get_boolean_opt('--debug')
    do_list = get_boolean_opt('-l')

    if "-t" in sys.argv:
        target_name = get_opt_with_arg('-t')
        try:
            target = target_class(target_name)
        except ValueError:
            error_out("'%s' is not a recognized target" % target_name)

    if "-d" in sys.argv:
        artifact_dir = get_opt_with_arg('-d')
        if not os.path.isdir(artifact_dir):
            error_out("'%s' does not exist: Invalid argument for -d" % artifact_dir)
        # make artifact_dir absolute, since we might change dirs
        artifact_dir = os.path.abspath(artifact_dir)

    if "-k" in sys.argv:
        kernel_dir = get_opt_with_arg('-k')
        if not os.path.isdir(kernel_dir):
            error_out("'%s' does not exist: Invalid argument for -k" % kernel_dir)
        if not os.path.isfile(kernel_dir + "/MAINTAINERS"):
            error_out("Missing 'MAINTAINERS'; '%s' does not not appear to be a kernel source directory" % kernel_dir)

    if "-m" in sys.argv:
        method_name = get_opt_with_arg('-m')

    if "-r" in sys.argv:
        report_run_id = get_opt_with_arg('-r')
        do_gen_report = True

    # done parsing command line
    # do more initialization

    # try to auto-detect the kernel source directory
    if not kernel_dir:
        wprint("No kernel source dir specified.")
        # try to find one
        if os.path.isfile("MAINTAINERS"):
            kernel_dir = "."
        if os.path.isfile("linux/MAINTAINERS"):
            kernel_dir = "linux"
        if kernel_dir:
            print("Using '%s' as the kernel source dir" % kernel_dir)

    methods = init_methods(kernel_dir)

    # handle '-l' before making artifact dir, it's just informational
    if do_list:
        list_methods(methods, target)
        sys.exit(0)

    # check for valid method_name
    if method_name:
        if method_name not in methods:
            error_out("Invalid argument for -m: method '%s' is not recognized (use -l to list)" % method_name)
        else:
            method = methods[method_name]

    # if no artifact-dir specified, create one in .
    if not artifact_dir:
        artifact_dir = "bttw-artifacts"
        try:
            os.mkdir(artifact_dir)
            artifact_dir_created = True
        except FileExistsError:
            pass

    # make artifact_dir absolute
    artifact_dir = os.path.abspath(artifact_dir)
    if target:
        target.artifact_dir = artifact_dir

    if not target:
        error_out("You must specify a target to optimize. Use -h for help.")

    # OK - we're doing an optimization run
    # set the run_prefix and kernel_id
    random.seed()
    rnd = random.randint(0, 999999);
    run_prefix = "run-%06d-" % rnd
    run_count = 1
    kernel_id = "%06d-%d" % (rnd, run_count)
    artifact_prefix = artifact_dir + "/" + run_prefix
    dprint(f"{artifact_prefix=}")
    target.artifact_prefix = artifact_prefix

    # generate report from results files, without running a test
    if do_gen_report:
        print(f"{saved_args=}")
        generate_report(report_run_id, method, target, artifact_dir, saved_args)

    if method:
        method.artifact_prefix = artifact_prefix
        method.save_baseline(target)

        try_one_method(method, target, artifact_prefix)

        # reset opt. status when not accumulating them
        method.restore_baseline(target)
        method.undo_optimization(target)

        # save results to a report file
        rheader = report_header(run_prefix[:-1], target.name, saved_args)
        log_to_report(artifact_prefix, rheader + method.results_txt)

        #method.show_results()
        print("Results are in the report file")
    else:
        tune_boot_time(methods, target, artifact_prefix)

    # FIXTHIS - should support an option to clean up working files
    print("Data files for this run are in '%s' with the prefix '%s'" % (artifact_dir, run_prefix))

    # FIXTHIS - need to fully reset target??
    # that would include restore_build_config, build, kinstall

    sys.exit(0)

if __name__ == "__main__":
    main()
