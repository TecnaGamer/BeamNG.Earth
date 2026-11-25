#![no_std]
#![allow(non_camel_case_types)]
#![allow(non_snake_case)]
#![allow(non_upper_case_globals)]
#![allow(deref_nullptr)]
include!(concat!(env!("OUT_DIR"), "/bindings.rs"));

#[cfg(test)]
mod tests {
    #[test]
    fn test_init_decompress() {
        // smoke test to check that we link to turbojpeg correctly
        unsafe {
            let handle = super::tjInitDecompress();
            assert!(!handle.is_null());
            super::tjDestroy(handle);
        }
    }
}
