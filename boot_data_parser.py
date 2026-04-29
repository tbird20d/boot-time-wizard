#!/usr/bin/python3
# boot_data_parser.py
#  This is a python module for parsing boot-data files

import sys
import os
import re
from datetime import date, time

debug = False

def set_debug(debug_flag):
    global debug
    debug = debug_flag

def dprint(msg):
    global debug
    if debug:
        print("DEBUG: " + msg)

# bdp classes, methods and functions:
#
#class boot_data_class:
#    __init__(filepath) - returns a boot data object
#    get(region_name, default):
#      returns the duration of the indicated region
#      region can be an initcall, in which case the name starts with "initcall_"
#    get_meta(meta_name, default="**missing**"):
#      returns a piece of meta-data from the boot data
#      config meta-data items start with "CONFIG_"
#    show() - show information about a boot data object
#
# function: parse_boot_data(req, filepath)
#   parse a boot data file, and report issues through the 'req' object
#   the req object must support the methods:
#     debug_log(msg), add_to_message(msg), and html_error(msg)
#
# class req_class: a compatiblity class for use with parse_boot_data()
#  example:
#   req = boot_data_parser.req_class()
#   bd = boot_data_parser.parse_boot_data(req, filepath)
#   bd.get(region_name, -99)
#   bd.get_meta("CPU_COUNT")
#   bd.show()


##### compatibility class: req ######
# define a compatibility class so that the boot data parsing code,
# can work in both a CGI context and not

class req_class:
    def __init__(self):
        return
    def debug_log(self, msg):
        dprint(msg)
    def add_to_message(self, msg):
        sys.stderr.write("Error: " + msg + "\n")
        sys.stderr.flush()
    def html_error(self, msg):
        RED = "\033[31m"
        RESET = "\033[0m"
        return RED + msg + RESET
    def test(self):
        global debug

        self.add_to_message("add_to_message: This text is output as a req.message()")
        print(self.html_error("html_error: This text should be red"))
        saved_debug = debug
        debug = True
        self.debug_log("debug_log: This is text for the debug log")
        debug = saved_debug

# class to hold boot data
class boot_data_class:
    def __init__(self, filepath):
        # eliminate symlinks in the path
        self.filepath = os.path.realpath(filepath)

        filename = os.path.basename(filepath)
        self.filename = filename

        # remove '-ref-values" from path if present
        # c_filename = converted filename
        c_filename = filename.replace("-ref-values","")

        try:
            lab_machine = c_filename[10:-18]
        except:
            lab_machine = "unknown_lab-unknown_machine"

        # Note: this parse doesn't work if lab name has a '-' in it
        # grab-boot-data.sh should convert '-' to '_' in the filename
        self.lab, self.machine = lab_machine.split("-",1)
        self.timestamp = c_filename[-17:-4]
        date_str, time_str = self.timestamp.split("-",1)
        year = int(date_str[0:2])+2000
        month = int(date_str[2:4])
        day = int(date_str[4:6])
        self.date = (year, month, day)
        dt = date(*self.date)
        self.date_str = dt.strftime("%d %b %Y")
        hour = int(time_str[0:2])
        minute = int(time_str[2:4])
        second = int(time_str[4:6])
        self.time = (hour, minute, second)
        t = time(*self.time)
        self.time_str = t.strftime("%H:%M:%S")
        # initcalls: key=initcall function name, value = duration
        self.initcalls = {}
        # region: key=reg_name, value = duration
        #    region is top-level directory (security, sound, init, kernel, etc.)
        self.regions = {}
        # reg_list = key = reg_name, value hold tuples with start and end lines
        #   for region sections that contributed to the region's total duration
        #   (start_line_no, start_line, end_line_no, end_line)
        self.reg_list = {}
        # region stack: key = reg_name, value holds tuples for open regions
        #   (start_time, end_type, line_no, line) (end_type = "any", "end")
        self.reg_stack = {}
        self.time_to_init = 999999
        self.CONFIGS = {}
        self.ARCH = "unknown"
        self.parse_errors = []

    def get(self, region_name, default):
        if region_name.startswith("initcall_"):
            ic_name = region_name[9:]
            if ic_name in self.initcalls:
                return self.initcalls[ic_name]
            else:
                return default
        if region_name in self.regions:
            return self.regions[region_name]
        else:
            return default

    def get_meta(self, meta_name, default="**missing**"):
        if meta_name.startswith("CONFIG_"):
            cname = meta_name[7:]
            return self.CONFIGS.get(cname, default)
        return self.__dict__.get(meta_name, default)

    def available_metas(self):
        metas = list(self.__dict__.keys())
        for name in ["initcalls", "CONFIGS", "regions", "reg_stack", "reg_list",
                     "CONFIG", "parse_errors"]:
            try:
                metas.remove(name)
            except ValueError:
                print("in available_metas: %s not in self.__dict__" % name)
        return metas

    def show(self):
        msg = "boot data for " + self.filename + "\n"
        for k in self.__dict__.keys():
            msg += "bd.%s='%s'" % (k, self.__dict__[k])
        return msg

# returns (initcall, duration), or (None, None)
# sample line:
#  [ 2624.555535] initcall hidpp_driver_init+0x0/0x1000 [hid_logitech_hidpp] returned 0 after 91992 usecs
# or
# [    0.035861] initcall 0x8000dd40 returned 0 after 0 usecs
#
def parse_initcall_line(req, line):
    #dprint("in parse_initcall_line: line='%s'" % line)
    m = re.match(".* initcall (.*) returned ([-0-9]+) after ([0-9]+) usecs", line)
    if not m:
        msg = "Problem parsing initcall line '%s'" % line.strip()
        req.add_to_message(msg)
        req.debug_log(msg)
        return (None, None)

    g = m.groups()
    location = g[0]
    retval = g[1]
    duration = g[2]

    # parse location
    if "+" in location:
        initcall, rest = location.split("+", 1)
    else:
        initcall = location

    try:
        duration = int(duration)
    except:
        req.debug_log("can't make duration '%d' into an int" % duration)
        return (None, None)
    return (initcall, duration)

def line_time_to_time(line):
    time_str, rest = line.split("]", 1)
    if not time_str.startswith("["):
        return 0.0
    else:
        return float(time_str[1:])

def line_time_to_microsecs(line):
    return int(line_time_to_time(line) * 1000000)

def parse_GBD_info(req, bd, block):
    for line in block:
        if line.startswith("ARGS=") or line.startswith("GBD_ARGS="):
            args_str = line.split("=", 1)[1].strip().strip('"')
            bd.GBD_ARGS = args_str

            # override filename-based lab and machine from ARGS
            args = args_str.split(" ")
            if "-l" in args:
                lab = args[args.index("-l") + 1]
                bd.lab = lab.replace("-", "_")
            if "-m" in args:
                machine = args[args.index("-m") + 1]
                bd.machine = machine
            continue

        if line.startswith("GBD_VERSION="):
            version_str = line.split("=", 1)[1].strip().strip('"')
            bd.GBD_VERSION = version_str
            continue


def parse_kernel_info(req, bd, block):
    for line in block:
        if line.startswith("KERNEL_VERSION="):
            version = line.split("=", 1)[1].strip(' \t\n\r"')
            bd.KERNEL_VERSION = version
            # calculate major, minor and revision
            kver_regex = "([0-9]+[.][0-9]+[.][0-9]+)(.*)"
            m = re.match(kver_regex, version)
            if m:
                bd.KVER_BASE = m.group(1)
                bd.KVER_EXTRA = m.group(2)
                bd.KVER_MAJOR, bd.KVER_MINOR, bd.KVER_REVISION = bd.KVER_BASE.split(".")
            continue

        if line.startswith("KERNEL_CMDLINE="):
            cmdline = line.split("=", 1)[1].strip().strip('"')
            bd.KERNEL_CMDLINE = cmdline
            bd.cmdline = cmdline
            if "quiet" in cmdline:
                bd.HAS_QUIET = "True"
            else:
                bd.HAS_QUIET = "False"
            if "initcall_debug" in cmdline:
                bd.HAS_INITCALL_DEBUG = "True"
            else:
                bd.HAS_INITCALL_DEBUG = "False"
            continue


# memory block should looks like this:
#              total        used        free      shared  buff/cache   available
#Mem:       32504388     2978444    14353020       64628    15172924    28993380
#Swap:      20971516     1259300    19712216
#
def parse_memory(req, bd, block):
    for line in block:
        if line.startswith("Mem:"):
            mem_fields = re.split(r'\s+', line)
            bd.MEM_TOTAL = mem_fields[1]
            bd.MEM_USED = mem_fields[2]
            return

    req.add_to_message(req.html_error("Invalid memory block in %s" % bd.filepath))
    return

def parse_cores(req, bd, block):
    cpu_num = 0
    total_cpu_mhz = 0.0
    total_bogomips = 0.0

    #req.add_to_message("block='%s'" % block)
    for line in block:
        if line.startswith("processor"):
            cpu_num += 1
            continue
        if line.startswith("cpu MHz"):
            total_cpu_mhz += float(line.split(":")[1].strip())
            continue
        if line.startswith("BogoMIPS") or line.startswith("bogomips"):
            total_bogomips += float(line.split(":")[1].strip())
            continue

    bd.CPU_COUNT = cpu_num
    bd.CPU_TOTAL_MHZ = total_cpu_mhz
    bd.CPU_TOTAL_BOGOMIPS = total_bogomips
    return

def parse_os(req, bd, block):
    #req.add_to_message("block='%s'" % block)
    for line in block:
        if "=" in line:
            name, value = line.lstrip().split("=", 1)
            value = value.strip('"')
            if name != "ISSUE":
                name = "OS_" + name
            setattr(bd, name, value)

    return

# has (printk match string, region name, printk source file, start or end indicator)
# start = printk indicates start of a region, end is another printk
# start* = printk indicates start of a region, printk besides 'in' ends region
# in = printk indicates interior of region, no effect on region state
# end = printk indicates the end of a region
reg_marker_printks = [
    ("rcu: Preemptible hierarchical RCU implementation", "rcu", "kernel/rcu/tree_plugin.h", "start"),
    ("RCU Tasks Trace: Setting shift to", "rcu", "kernel/rcu/tasks.h", "end"),
    ("Calibrating delay loop", "init", "init/calibrate.c", "start*"),
    ("LSM: initializing lsm=", "security", "security/security.c", "start*"),
    ("smp: Bringing up secondary CPUs", "kernel", "kernel/smp.c", "start"),
    ("CPU: All CPU(s) started at EL", "kernel", "arch/arm64/kernel/smp.c", "end"),
    ("Zone ranges:", "mm", "mm/mm_init.c", "start"),
    ("Initmem setup node", "mm", "mm/mm_init.c", "end"),
    ]

def record_region(req, bd, line_no, line):
    # maintain a list of regions and their durations
    # try to match printk to a region start or end
    for reg_str, reg_name, source_file, delim_type in reg_marker_printks:
        if reg_str in line:
            line_time = line_time_to_microsecs(line)
            if delim_type == "start":
                if reg_name in bd.reg_stack:
                    # save error if region is already started
                    bd.parse_errors.append("record_region: reg_name '%s' found nested at line '%s'" % (reg_name, line))
                else:
                    # record start time for this region
                    bd.reg_stack[reg_name] = (line_time, "end", line_no, line)
                    req.debug_log("DEBUG: adding '%s'(end) to reg_stack: line_time=%s" % (reg_name, line_time))
                    return

            elif delim_type == "start*":
                if reg_name in bd.reg_stack:
                    # save error if region is already started
                    bd.parse_errors.append("reg_name '%s' found nested at line '%s'" % (reg_name, line))
                else:
                    # record start time for this region
                    bd.reg_stack[reg_name] = (line_time, "any", line_no, line)
                    req.debug_log("DEBUG: adding '%s'(any) to reg_stack: line_time=%s" % (reg_name, line_time))
                    return

            elif delim_type == "end":
                if reg_name not in bd.reg_stack:
                    bd.parse_errors.append("found end string for region '%s' without matching start, at line '%s'" % (reg_name, line))
                else:
                    # remove region from reg_stack
                    start_time, delim_type, start_line_no, start_line = bd.reg_stack.pop(reg_name)
                    duration = line_time - start_time
                    req.debug_log("DEBUG: removing '%s' to reg_stack: line_time=%s" % (reg_name, line_time))

                    # record duration for region
                    range_t = (start_line_no, start_line, line_no, line)
                    if reg_name in bd.regions:
                        bd.regions[reg_name] += duration
                        bd.reg_list[reg_name].append(range_t)
                    else:
                        bd.regions[reg_name] = duration
                        bd.reg_list[reg_name] = [range_t]
                    req.debug_log("DEBUG: adding duration %s to region '%s'" % (duration, reg_name))
            elif delim_type == "in":
                # save error if we're not in the expected region
                if reg_name not in bd.reg_stack:
                    bd.parse_errors.append("found 'in' string outside region '%s', at line '%s'" % (reg_name, line))
            else:
                bd.parse_errors.append("encountered unknown region delimiter type of '%s'" % delim_type)

    # if no match, but we have open 'start*' regions, end them
    rs_keys = list(bd.reg_stack.keys())
    for reg_name in rs_keys:
        start_time, end_type, start_line_no, start_line = bd.reg_stack[reg_name]
        if end_type == "any":
            line_time = line_time_to_microsecs(line)
            duration = line_time - start_time
            del bd.reg_stack[reg_name]

            # record duration for region
            range_t = (start_line_no, start_line, line_no, line)
            if reg_name in bd.regions:
                bd.regions[reg_name] += duration
                bd.reg_list[reg_name].append(range_t)
            else:
                bd.regions[reg_name] = duration
                bd.reg_list[reg_name] = [range_t]
            req.debug_log("DEBUG: adding duration %s to region '%s'" % (duration, reg_name))

# returns boot data, which is an instance containing
#   meta-data and initcalls and regions duration data
# 'bd' = boot data
def parse_boot_data(req, filepath):
    bd = boot_data_class(filepath)
    if not req:
        req = req_class()

    req.debug_log("filepath=%s" % filepath)

    # TRB test exceptions here
    #if 'test' in filepath:
    #    # generate a type exception
    #    foo = bd * 5

    fd = open(filepath, "r")
    sections = {}
    section = ""
    block = []

    line_no = 0
    for line in fd.readlines():
        line_no += 1
        # handle section switches
        if line.startswith("== "):
            if section:
                sections[section] = block

            # wrap up last section
            if section == "Kernel Info":
                parse_kernel_info(req, bd, block)
            elif section == "GRAB-BOOT-DATA INFO":
                parse_GBD_info(req, bd, block)
            elif section == "MEMORY":
                parse_memory(req, bd, block)
            elif section == "CORES":
                parse_cores(req, bd, block)
            elif section == "OS":
                parse_os(req, bd, block)
            elif section == "CONFIG":
                bd.CONFIG = block

            new_section = line[3:].strip()
            if not new_section.endswith(" =="):
                req.add_to_message(req.html_error("malformed section '%s' in file %s" % (section, filepath)))
                continue

            section = new_section[:-3]
            block = []
            continue
        else:
            if section == "CONFIG":
                line = line.strip()
                # skip blank and comment lines
                if not line:
                    continue
                if line.startswith("#") and not line.endswith(" is not set"):
                    continue
                if line.startswith("CONFIG_"):
                    name, value = line[7:].split("=", 1)
                    bd.CONFIGS[name] = value
                elif line.endswith(" is not set"):
                    name = line[9:].split(" ")[0]
                    bd.CONFIGS[name] = "n"
                else:
                    req.debug_log("weirdness in config for line: '%s'" % line)

            block.append(line.strip())

        # "KERNEL_MESSAGES" section - parse directly in this section
        if "initcall " in line and "returned " in line:
            #req.debug_log("line=%s" % line)
            (initcall, duration) = parse_initcall_line(req, line)
            if not initcall:
                req.debug_log("problem parsing initcall")
            else:
                bd.initcalls[initcall] = duration
            continue

        if "Machine model:" in line:
            junk, machine = line.split("Machine model:",1)
            bd.MACHINE = machine.strip()

        if "Run /init as init process" in line:
            time = line_time_to_time(line)
            bd.time_to_init = time

        if "Run /sbin/init as init process" in line:
            time = line_time_to_time(line)
            bd.time_to_init = time

        # do region detection in the printks
        if line.startswith("["):
            # FIXTHIS - could ignore lines with 0.000000 timestamps here?
            req.debug_log("%d: %s" % (line_no, line.strip()))
            record_region(req, bd, line_no, line.strip())
            req.debug_log("   regions = '%s'" % bd.regions)
            req.debug_log("   reg_stack = '%s'" % bd.reg_stack)
            req.debug_log("   reg_list = '%s'" % bd.reg_list)

    req.debug_log("regions = '%s'" % bd.regions)

    # if anything still on reg_stack, report an error
    if bd.reg_stack:
        req.debug_log("Error in parsing regions: reg_stack after parse='%s'" % bd.reg_stack)
        req.debug_log("Error in parsing regions: reg_stack after parse='%s'" % bd.reg_stack)

    # detect ARCH from config values
    bd.ARCH = get_arch(req, bd)

    if bd.parse_errors:
        req.debug_log("parse errors: %s" % bd.parse_errors)

    return bd

POSSIBLE_ARCHES = ["X86_64", "ARM64", "RISCV", "ARM", "X86_32",
    "ALPHA", "ARC", "SCKY", "HEXAGON", "LOONGARCH", "M86K", "MICROBLAZE",
    "MIPS", "NIOS2", "OPENRISC", "PARISC", "PPC32", "PPC64", "SUPERH", "UML",
    "XTENSA",
  ]

def get_arch(req, bd):
    for arch in POSSIBLE_ARCHES:
        if arch in bd.CONFIGS:
            dprint("get_arch() returning %s" % arch)
            return arch

    msg = req.html_error("Unknown arch for boot data: %s" % bd.filename)
    req.add_to_message(msg)
    req.debug_log(msg)
    return "unknown-arch"

