# tclConfig.sh --
#
# This shell script (for sh) is generated automatically by Tcl's
# configure script.  It will create shell variables for most of
# the configuration options discovered by the configure script.
# This script is intended to be included by the configure scripts
# for Tcl extensions so that they don't have to figure this all
# out for themselves.
#
# The information in this file is specific to a single platform.

TCL_DLL_FILE="tcl90.dll"

# Tcl's version number.
TCL_VERSION='9.0'
TCL_MAJOR_VERSION='9'
TCL_MINOR_VERSION='0'
TCL_PATCH_LEVEL='9.0.4'

# C compiler to use for compilation.
TCL_CC='cl'

# -D flags for use with the C compiler.
TCL_DEFS='-nologo -c   /DHAVE_CPUID=1 -W3 -wd4311 -wd4312 -wd4090 -wd4146 -wd4311 -wd4312 -FpD:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\Release_AMD64_VC13\tcl_Dynamic\  -fp:strict -O2 -GS -GL -MD -I"D:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\..\win" -I"D:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\..\generic"  -I"D:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\..\libtommath"  /DMP_PREC=4 /Dinline=__inline /D_CRT_SECURE_NO_DEPRECATE /D_CRT_NONSTDC_NO_DEPRECATE /DMP_FIXED_CUTOFFS /DSTDC_HEADERS /DUSE_NMAKE=1 /DHAVE_STDINT_H=1 /DHAVE_INTTYPES_H=1 /DHAVE_STDBOOL_H=1 /DTCL_WITH_EXTERNAL_TOMMATH /DMP_64BIT /DNDEBUG /DTCL_CFG_OPTIMIZED /DTCL_CFG_DO64BIT   /DBUILD_tcl'

# Default flags used in an optimized and debuggable build, respectively.
TCL_CFLAGS_DEBUG='-nologo -c -W3 -YX -FpD:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\Release_AMD64_VC13\tcl_Dynamic\ -MDd'
TCL_CFLAGS_OPTIMIZE='-nologo -c -W3 -YX -FpD:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\Release_AMD64_VC13\tcl_Dynamic\ -MD'

# Default linker flags used in an optimized and debuggable build, respectively.
TCL_LDFLAGS_DEBUG='-nologo -machine:AMD64 -debug -debugtype:cv'
TCL_LDFLAGS_OPTIMIZE='-nologo -machine:AMD64 -release -opt:ref -opt:icf,3'

# Flag, 1: we built a shared lib, 0 we didn't
TCL_SHARED_BUILD=1

# The name of the Tcl library (may be either a .a file or a shared library):
TCL_LIB_FILE='tcl90.lib'

# The name of a zip containing the /library and /encodings (may be either a .zip file or a shared library):
TCL_ZIP_FILE='@TCL_ZIP_FILE@'

# Flag to indicate whether shared libraries need export files.
TCL_NEEDS_EXP_FILE=''

# Additional libraries to use when linking Tcl.
TCL_LIBS='kernel32.lib advapi32.lib netapi32.lib user32.lib userenv.lib ws2_32.lib ucrt.lib netapi32.lib user32.lib userenv.lib ws2_32.lib'

# Top-level directory in which Tcl's platform-independent files are
# installed.
TCL_PREFIX='D:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64'

# Top-level directory in which Tcl's platform-specific files (e.g.
# executables) are installed.
TCL_EXEC_PREFIX='D:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64\bin'

# Flags to pass to cc when compiling the components of a shared library:
TCL_SHLIB_CFLAGS=''

# Flags to pass to cc to get warning messages
TCL_CFLAGS_WARNING='-W3'

# Extra flags to pass to cc:
TCL_EXTRA_CFLAGS='-YX'

# Base command to use for combining object files into a shared library:
TCL_SHLIB_LD='link -nologo -machine:AMD64  -ltcg -release -opt:ref -opt:icf,3 -nodefaultlib:ucrt.lib -dll'

# Base command to use for combining object files into a static library:
TCL_STLIB_LD='lib -nologo'

# Either '$LIBS' (if dependent libraries should be included when linking
# shared libraries) or an empty string.  See Tcl's configure.ac for more
# explanation.
TCL_SHLIB_LD_LIBS='kernel32.lib advapi32.lib netapi32.lib user32.lib userenv.lib ws2_32.lib ucrt.lib netapi32.lib user32.lib userenv.lib ws2_32.lib'

# Suffix to use for the name of a shared library.
TCL_SHLIB_SUFFIX='.dll'

# Library file(s) to include in tclsh and other base applications
# in order to provide facilities needed by DLOBJ above.
TCL_DL_LIBS=''

# Flags to pass to the compiler when linking object files into
# an executable tclsh or tcltest binary.
TCL_LD_FLAGS=''

# Flags to pass to cc/ld, such as "-R /usr/local/tcl/lib", that tell the
# run-time dynamic linker where to look for shared libraries such as
# libtcl.so.  Used when linking applications.  Only works if there
# is a variable "LIB_RUNTIME_DIR" defined in the Makefile.
TCL_CC_SEARCH_FLAGS=''
TCL_LD_SEARCH_FLAGS=''

# Additional object files linked with Tcl to provide compatibility
# with standard facilities from ANSI C or POSIX.
TCL_COMPAT_OBJS=''

# Name of the ranlib program to use.
TCL_RANLIB=''

# -l flag to pass to the linker to pick up the Tcl library
TCL_LIB_FLAG='tcl90.lib'

# String to pass to linker to pick up the Tcl library from its
# build directory.
TCL_BUILD_LIB_SPEC='D:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\Release_AMD64_VC13\tcl90.lib'

# String to pass to linker to pick up the Tcl library from its
# installed directory.
TCL_LIB_SPEC='D:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64\lib\tcl90.lib'

# String to pass to the compiler so that an extension can
# find installed Tcl headers.
TCL_INCLUDE_SPEC='-ID:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64\include'

# Indicates whether a version numbers should be used in -l switches
# ("ok" means it's safe to use switches like -ltcl7.5;  "nodots" means
# use switches like -ltcl75).  SunOS and FreeBSD require "nodots", for
# example.
TCL_LIB_VERSIONS_OK='nodots'

# String that can be evaluated to generate the part of a shared library
# name that comes after the "libxxx" (includes version number, if any,
# extension, and anything else needed).  May depend on the variables
# VERSION and SHLIB_SUFFIX.  On most UNIX systems this is
# ${VERSION}${SHLIB_SUFFIX}.
TCL_SHARED_LIB_SUFFIX='90.dll'

# String that can be evaluated to generate the part of an unshared library
# name that comes after the "libxxx" (includes version number, if any,
# extension, and anything else needed).  May depend on the variable
# VERSION.  On most UNIX systems this is ${VERSION}.a.
TCL_UNSHARED_LIB_SUFFIX='90.lib'

# Location of the top-level source directory from which Tcl was built.
# This is the directory that contains a README file as well as
# subdirectories such as generic, unix, etc.  If Tcl was compiled in a
# different place than the directory containing the source files, this
# points to the location of the sources, not the location where Tcl was
# compiled.
TCL_SRC_DIR='D:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\..'

# List of standard directories in which to look for packages during
# "package require" commands.  Contains the "prefix" directory plus also
# the "exec_prefix" directory, if it is different.
TCL_PACKAGE_PATH='D:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64\lib'

# Tcl supports stub.
TCL_SUPPORTS_STUBS=1

# The name of the Tcl stub library (.a):
TCL_STUB_LIB_FILE='tclstub.lib'

# -l flag to pass to the linker to pick up the Tcl stub library
TCL_STUB_LIB_FLAG='tclstub.lib'

# String to pass to linker to pick up the Tcl stub library from its
# build directory.
TCL_BUILD_STUB_LIB_SPEC='-LD:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\Release_AMD64_VC13 tclstub.lib'

# String to pass to linker to pick up the Tcl stub library from its
# installed directory.
TCL_STUB_LIB_SPEC='-LD:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64\lib tclstub.lib'

# Path to the Tcl stub library in the build directory.
TCL_BUILD_STUB_LIB_PATH='D:\Python-3.15.0rc1\externals\tcl-9.0.4.0\win\Release_AMD64_VC13\tclstub.lib'

# Path to the Tcl stub library in the install directory.
TCL_STUB_LIB_PATH='D:\Python-3.15.0rc1\PCbuild\\..\externals\tcltk-9.0.4.0\amd64\lib\tclstub.lib'

# Name of the zlib library that extensions should use
TCL_ZLIB_LIB_NAME='zdll.lib'

# Name of the tommath library that extensions should use
TCL_TOMMATH_LIB_NAME='tommath.lib'
