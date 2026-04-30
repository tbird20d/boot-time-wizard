#!/bin/sh
# SPDX-License-Identifier: 0BSD
#
# grab-boot-data.sh
#  This is a simple script to grab boot time data from dmesg
#
# Author: Tim Bird <tim.bird@sony.com>
# Copyright 2024 Sony Electronics. Inc.
#
# gather meta-data about the machine, so the effects of things like
# memory, cpu count, cpu frequency, bogomips, kernel command-line options,
# etc. can be correlated with the duration of specific boot operations
#
# Changelog:
#  Version 1.5 - add timestamp to GBD section, move systemd section higher
#                handle missing 'sudo'
#  Version 1.4 - add systemd info section
#  Version 1.3 - don't use ps -A on systems where -A is not supported
#  Version 1.2 - replace - with _ in lab name
#  Version 1.1 - fix shellcheck issues, change SKIP_ vars to DO_ vars
#  Version 1.0 - first release

VERSION="1.5"

# for testing
#UPLOAD_URL="http://localhost:8000/cgi-bin/tbwiki.cgi/Boot_Data?action=do_upload"
UPLOAD_URL="https://birdcloud.org/boot-time/Boot_Data?action=do_upload"

usage() {
    cat <<HERE
Usage: grab-boot-data.sh -l <lab> -m <machine> [options]

Collect machine information and boot data, and send it to the
birdcloud boot-time wiki, for analysis.

Normally, you run this soon after booting your machine.  You should
add the following kernel command line options to your boot configuration:
  quiet
  initcall_debug
  log_buf_len=10M

You may need to add these to a booloader configuration file, such
as grub.cfg (if using the 'grub' bootloader, or if using u-boot,
by editing the bootargs variable). See bootloader-specific documentation
for how to adjust the kernel command line for your boot.

Options:
 -h,--help  Show this online usage help
 -s         Skip kernel command-line checks
            Use this if you want to collect machine data and dmesg data,
            but didn't use 'quiet' or 'initcall_debug' on the kernel
            command line.

 -l <lab>   Specify lab (or user) name for data
 -m <name>  Specify machine name for data

 -d <dir>   Output the boot-data file to the indicated directory

 -x         Don't upload the data to the wiki (but still save the data
            to a file)

 -u <file>  Upload specified boot-data file to the wiki
            This should be used when the target doesn't have networking or
            is missing the curl command. In this case, use '-x' when
            running on the target machine, transfer the resulting
            data file to a machine that does have networking and curl,
            and then upload the file using -u.

 -V,--version Show program version information
 --debug    Show debug information while running
HERE
}

TIMESTAMP_STR=$(date +"%y%m%d-%H%M%S")

OUTPUT_DIR=.

LAB="unknown"
MACHINE="unknown"

SAVE_ARGS="$*"

DO_CMDLINE_CHECKS="yes"
DO_GRAB="yes"
DO_UPLOAD="yes"

# parse options
while [ -n "$1" ] ; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -s)
            echo "Skipping command line checks"
            DO_CMDLINE_CHECKS=
            shift
            ;;
        -x)
            echo "Skipping upload"
            DO_UPLOAD=
            shift
            ;;
        -d)
            shift
            OUTPUT_DIR="$1"
            shift
            ;;
        -l)
            shift
            LAB="$(echo "$1" | tr - _ )"
            shift
            ;;
        -m)
            shift
            MACHINE="$1"
            shift
            ;;
        -u)
            shift
            DO_CMDLINE_CHECKS=
            DO_GRAB=
            UPLOAD_FILE=$1
            shift
            ;;
        -V|--version)
            echo "grab-boot-data.sh Version $VERSION"
            exit 0
            ;;
        --debug)
            set -x
            shift
            ;;
        *)
            echo "Unrecognized command line option '$1'"
            echo "Use -h for help"
            exit 1
            ;;
    esac
done

OUTFILE="boot-data-${LAB}-${MACHINE}-${TIMESTAMP_STR}.txt"
OUTPATH="${OUTPUT_DIR}/${OUTFILE}"

# do some error checking
if [ -n "$DO_GRAB" -a "$LAB" = "unknown" ] ; then
    echo "Error: Please specify a lab name for the boot data"
    exit 1
fi

if [ -n "$DO_GRAB" -a "$MACHINE" = "unknown" ] ; then
    echo "Error: Please specify a machine name for the boot data"
    exit 1
fi

# check if 'quiet' and 'initcall_debug' are in the kernel command line
CMDLINE="$(cat /proc/cmdline)"

if [ -n "$DO_CMDLINE_CHECKS" -a "${CMDLINE#*quiet}" = "${CMDLINE}" ] ; then
    echo "Error: Missing 'quiet' on kernel command line"
    echo "Please reboot the kernel with 'quiet' on the command line, and run again"
    exit 1
fi

if [ -n "$DO_CMDLINE_CHECKS" -a "${CMDLINE#*initcall_debug}" = "${CMDLINE}" ] ; then
    echo "Error: Missing 'initcall_debug' on kernel command line"
    echo "Please reboot the kernel with 'initcall_debug' on the command line, and run again"
    exit 1
fi

out_section() {
    echo "== $1 ==" >>"$OUTPATH"
    $2 >>"$OUTPATH" 2>&1
    echo >>"$OUTPATH"
}

get_distro() {
    if [ -f /etc/os-release ] ; then
        echo -n "OS_RELEASE:"
        cat /etc/os-release
    fi
    if [ -f /etc/issue ] ; then
        echo -n "ISSUE="
        cat /etc/issue
    fi
}

get_processes() {
    # ps -A doesn't work with some busybox builds
    if ps -A 2>&1 | grep "invalid option" >/dev/null ; then
        ps
    else
        ps -A
    fi
}

get_config() {
    # check a few different places for the kernel config file
    if [ -f /proc/config.gz ] ; then
        zcat /proc/config.gz
        return
    fi

    # check /lib/modules
    RELEASE="$(uname -r)"
    if [ -f "/lib/modules/${RELEASE}/kernel/kernel/configs.ko" ] ; then
        insmod "/lib/modules/${RELEASE}/kernel/kernel/configs.ko"
        zcat /proc/config.gz
        rmmod configs
        return
    fi
    if [ -f "/lib/modules/${RELEASE}/kernel/kernel/configs.ko.xz" ] ; then
        insmod "/lib/modules/${RELEASE}/kernel/kernel/configs.ko.xz"
        zcat /proc/config.gz
        rmmod configs
        return
    fi
    if [ -f "/lib/modules/${RELEASE}/build/.config" ] ; then
        cat "/lib/modules/${RELEASE}/build/.config"
        return
    fi

    # check /boot directory
    if [ -f "/boot/config-${RELEASE}" ] ; then
        cat "/boot/config-${RELEASE}"
        return
    fi
    if [ -f /boot/config ] ; then
        cat /boot/config
        return
    fi

    # other possibilities, though these are less likely on embedded devices
    # IMHO - it's too easy for these to be inaccurate, comment them
    # out for now
    # /usr/src/linux-$RELEASE/.config
    # /usr/src/linux/.config
    # /usr/lib/ostree-boot/config-$RELEASE
    # /usr/lib/kernel/config-$RELEASE
    # /usr/src/linux-headers-$RELEASE/.config",

    echo "Can't find kernel config data or file"
}

if [ -n "$DO_GRAB" ] ; then
    echo "=== Machine Info ==============================" >"$OUTPATH"

    out_section UPTIME "uptime"

    echo "== GRAB-BOOT-DATA INFO ==" >>"$OUTPATH"
    echo "GBD_ARGS=\"$SAVE_ARGS\"" >>"$OUTPATH"
    echo "GBD_VERSION=\"$VERSION\"" >>"$OUTPATH"
    echo "GBD_TIMESTAMP=\"$TIMESTAMP_STR\"" >>"$OUTPATH"
    echo "GBD_LAB=\"$LAB\"" >>"$OUTPATH"
    echo "GBD_MACHINE=\"$MACHINE\"" >>"$OUTPATH"
    echo >>"$OUTPATH"

    echo "== Kernel Info ==" >>"$OUTPATH"
    echo "KERNEL_VERSION=\"$(uname -r -v)\"" >>"$OUTPATH"
    echo "KERNEL_CMDLINE=\"$CMDLINE\"" >>"$OUTPATH"
    echo >>"$OUTPATH"

    out_section OS "get_distro"
    out_section MEMORY "free"
    out_section "DISK USAGE" "df -h"
    out_section MOUNTS "mount"
    out_section PROCESSES "get_processes"

    #out_section INTERRUPTS "cat /proc/interrupts"
    out_section CORES "cat /proc/cpuinfo"
    out_section CONFIG "get_config"

    echo "== SYSTEMD INFO ==" >>"$OUTPATH"
    if [ -x /usr/bin/systemd-analyze ] ; then
        systemd-analyze >> "$OUTPATH"
        echo ----- >>"$OUTPATH"
        systemd-analyze blame >>"$OUTPATH"
    else
        echo "NOTE: systemd-analyze is not present on this system" >> "$OUTPATH"
    fi

    if [ -x /usr/bin/sudo ] ; then
        out_section "KERNEL MESSAGES" "sudo dmesg"
    else
        out_section "KERNEL MESSAGES" "dmesg"
    fi

    echo >>"$OUTPATH"

    echo "Boot data is in the file: $OUTPATH"
    UPLOAD_FILE="${OUTPATH}"
fi

### upload the data
if [ -n "$DO_UPLOAD" ] ; then
    echo "Upload file=$UPLOAD_FILE"

    RESULT_HTML_FILE="$(mktemp)"
    echo "Uploading data ..."
    curl -F submit_button=Upload -F "file_1=@$UPLOAD_FILE" -F file_2= -F file_3= "$UPLOAD_URL" -o "$RESULT_HTML_FILE"
    if grep "uploaded successfully" "$RESULT_HTML_FILE" >/dev/null 2>&1 ; then
        echo "Data uploaded successfully."
        rm "$RESULT_HTML_FILE"
    else
        echo "Error: There was a problem uploading the data"
        echo "See $RESULT_HTML_FILE for details."
        echo "Don't forget to remove this file when done using it."
   fi
fi
