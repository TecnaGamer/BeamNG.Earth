/* build-only glue for mingw/clang (error.c is written for MSVC/ReactOS headers); error.c itself is unmodified */
#include <windows.h>
#include <winternl.h>
#include <intrin.h>
NTSYSAPI NTSTATUS NTAPI LdrFindEntryForAddress(PVOID Address, PLDR_DATA_TABLE_ENTRY *Entry);
NTSYSAPI VOID NTAPI RtlCopyUnicodeString(PUNICODE_STRING Dest, PCUNICODE_STRING Src);
#define _ReturnAddress() __builtin_return_address(0)
