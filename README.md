This is the Boot-Time Wizard, a test program for
researching boot-time tuning options for embedded Linux systems.

# Introduction
This tool provides a number of features that support developers
working to improve boot-time on their embedded Linux systems.

 * `grab-boot-data.sh` is a tool to collect information about a system
   and it's boot time.  This data is used for subsequent analysis.
 * `boot_data_parser.py` and `boot-data` are used to process information
   from a 'boot-data' file, and present info to the user
 * `boot-time-wizard` is a program that allows a developer to experiment
   with a pre-defined set of boot-time optimization techniques

## boot-time-wizard
This program runs a series of tests, doing the following for each
selected optimization technique:
 * instrumenting the Linux kernel to retrieve information relevant
   to the particular optimization technique selected
 * building, installing, and booting the instrumented kernel
 * gathering baseline boot-time data
 * applying the specified optimization technique
 * building, installing, (if appropriate) and booting an optimized kernel
 * gathering results boot-time data
 * comparing the baseline data with the results data to create a report
   of changes to the system

This system allows you to see what optimization techniques are effective
for your platform (kernel, distro and required early boot functionality).
It also allows you to see what breaks if you use a particular
optimization technique.

## Dependencies
The boot-time-wizard uses `'ttc'` (see https://github.com/tbird/ttc) as
its board access and management tool.  You should have configured
ttc with full support for: 1) building and install a kernel,
2) accessing a board, and controlling its power and boot, 3) manipulating
the kernel command line.

# Getting Started
Use `boot-time-wizard -t <target> -l` to list the optimization methods
that are available to try on your system.

Then use `boot-time-wizard -t <target> -m <method>` to test that
optimization method.  Data will be collected from your system
as the method is tried, and a report will be provided of the effect
on the system.  These appear in an 'artifact directory', usually
called `btw-artifacts` in the current directory.

Use `boot-time-wizard -h` to get usage help for the tool.

Happy Linuxing!!...
