/*
 * Copyright (C) 2025 EAZY BLACK (https://git.chefkiss.dev/EAZYBLACK)
 * 
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 * 
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#include <windows.h>
#include <winternl.h>
#include <initguid.h>
#include <oaidl.h>

#define SafeRelease(Interface) do { if (Interface) { (Interface)->lpVtbl->Release(Interface); (Interface) = NULL; } } while (0)
typedef struct IRestrictedErrorInfoVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(void *This, REFIID riid, void **ppvObject);
    ULONG (STDMETHODCALLTYPE *AddRef)(void *This);
    ULONG (STDMETHODCALLTYPE *Release)(void *This);
} IRestrictedErrorInfoVtbl;

typedef struct IRestrictedErrorInfo {
    IRestrictedErrorInfoVtbl *lpVtbl;
} IRestrictedErrorInfo;

DEFINE_GUID(IID_IInternalErrorInfo, 0x66818B96, 0xDC17, 0x4C12, 0x8C, 0xA1, 0x8E, 0x1F, 0xBA, 0xA5, 0xBF, 0x80);

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved)
{
    return TRUE;
}

typedef struct _HSTRING_HEADER {
	ULONG	Flags;		
	ULONG	Length;		
	ULONG	Padding1;
	ULONG	Padding2;
	PCWSTR	StringRef;
} HSTRING_HEADER, *HSTRING;

BOOL WINAPI RoOriginateErrorW(
	IN	HRESULT	Result,
	IN	ULONG	Length,
	IN	PCWSTR	Message OPTIONAL)
{
	return TRUE;
}

HRESULT WINAPI SetRestrictedErrorInfo(
	IN	IRestrictedErrorInfo *RestrictedErrorInfo OPTIONAL)
{
	HRESULT Result;
	IErrorInfo *ErrorInfo;
	IUnknown *InternalErrorInfo;

	Result = S_OK;
	ErrorInfo = NULL;

	if (RestrictedErrorInfo) {
		InternalErrorInfo = NULL;
		
		Result = RestrictedErrorInfo->lpVtbl->QueryInterface(
			RestrictedErrorInfo,
			&IID_IInternalErrorInfo,
			(void**) &InternalErrorInfo);
		
		if (Result == E_NOINTERFACE) {
			Result = E_INVALIDARG;
			RoOriginateErrorW(Result, 0, L"RestrictedErrorInfo");
		}
		
		SafeRelease(InternalErrorInfo);
		
		if (FAILED(Result)) {
			goto Exit;
		}

		Result = RestrictedErrorInfo->lpVtbl->QueryInterface(
			RestrictedErrorInfo,
			&IID_IErrorInfo,
			(void**) &ErrorInfo);
	}

	if (SUCCEEDED(Result)) {
		Result = SetErrorInfo(0, ErrorInfo);
	}

Exit:
	SafeRelease(ErrorInfo);
	return Result;
}

HRESULT WINAPI GetRestrictedErrorInfo(
	OUT	IUnknown	**RestrictedErrorInfo)
{
	return S_FALSE;
}

BOOL WINAPI RoOriginateLanguageException(
	IN	HRESULT		Result,
	IN	HSTRING		Message,
	IN	IUnknown	*LanguageException OPTIONAL)
{
	NTSTATUS Status;
	PLDR_DATA_TABLE_ENTRY Entry;
	UNICODE_STRING FaultingModuleName;
	WCHAR Buffer[MAX_PATH];
	PVOID Address;

	if (SUCCEEDED(Result)) {
		return FALSE;
	}

	FaultingModuleName.Buffer = Buffer;
	FaultingModuleName.Length = 0;
	FaultingModuleName.MaximumLength = sizeof(Buffer);

	Address = _ReturnAddress();
	
	if (Address) {
		Status = LdrFindEntryForAddress(Address, &Entry);
		
		if (NT_SUCCESS(Status)) {
			if (Entry->FullDllName.Length <= FaultingModuleName.MaximumLength) {
				RtlCopyUnicodeString(&FaultingModuleName, &Entry->FullDllName);
			}
		}
	}

	if (FaultingModuleName.Length == 0) {
		RtlInitUnicodeString(&FaultingModuleName, L"");
	}

	return TRUE;
}