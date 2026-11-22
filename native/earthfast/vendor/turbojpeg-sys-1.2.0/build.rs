#![allow(unused_imports, dead_code)]
use anyhow::{Result, Context as _, anyhow, bail};
use std::{env, fs};
use std::collections::HashMap;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Command;

fn main() -> Result<()> {
    println!("cargo:rerun-if-changed=build.rs");
    let link_kind = get_link_kind()?;
    let library = build_or_find_library(link_kind)?;
    generate_or_copy_bindings(&library)?;

    if !library.include_paths.is_empty() {
        // "cargo:include={}" is equivalent to "cargo::metadata=include={}", but "cargo::metadata"
        // requires a higher Rust version.
        //
        // we use the same convention as `libz-sys` and separate multiple include paths by a comma.
        let include_paths = library.include_paths.iter()
            .map(|p| p.display().to_string()).collect::<Vec<_>>();
        println!("cargo:include={}", include_paths.join(","));
    }

    Ok(())
}

#[derive(Debug)]
enum LinkKind {
    Static,
    Dynamic,
    Default,
}

fn get_link_kind() -> Result<LinkKind> {
    let static_env = env_bool("TURBOJPEG_STATIC")?;
    let dynamic_env = env_bool("TURBOJPEG_DYNAMIC")?.or(env_bool("TURBOJPEG_SHARED")?);

    match (static_env, dynamic_env) {
        (Some(true), Some(true)) =>
            bail!("Both TURBOJPEG_STATIC and TURBOJPEG_DYNAMIC/TURBOJPEG_SHARED are set to 1"),
        (Some(false), Some(false)) =>
            bail!("Both TURBOJPEG_STATIC and TURBOJPEG_DYNAMIC/TURBOJPEG_SHARED are set to 0"),
        (None, None) =>
            Ok(LinkKind::Default),
        (Some(true) | None, Some(false) | None) =>
            Ok(LinkKind::Static),
        (Some(false) | None, Some(true) | None) =>
            Ok(LinkKind::Dynamic),
    }
}


#[derive(Debug)]
struct Library {
    include_paths: Vec<PathBuf>,
    defines: HashMap<String, Option<String>>,
}

fn build_or_find_library(link_kind: LinkKind) -> Result<Library> {
    match env("TURBOJPEG_SOURCE") {
        Some(source) => {
            if source.eq_ignore_ascii_case("vendor") {
                build_vendor(link_kind)
            } else if source.eq_ignore_ascii_case("pkg-config") ||
                source.eq_ignore_ascii_case("pkgconfig") ||
                source.eq_ignore_ascii_case("pkgconf")
            {
                find_pkg_config(link_kind)
            } else if source.eq_ignore_ascii_case("explicit") {
                find_explicit(link_kind)
            } else {
                bail!("Unknown value of TURBOJPEG_SOURCE, supported values are:\n\
                    - 'vendor' to build the library from source bundled with the turbojpeg-sys crate,\n\
                    - 'pkg-config' to find the library using pkg-config,\n\
                    - 'explicit' to use TURBOJPEG_LIB_DIR and TURBOJPEG_INCLUDE_DIR")
            }
        },
        None => {
            if cfg!(feature = "cmake") {
                build_vendor(link_kind)
            } else if cfg!(feature = "pkg-config") {
                find_pkg_config(link_kind)
            } else {
                find_explicit(link_kind)
            }
        },
    }
}

#[cfg(feature = "pkg-config")]
fn find_pkg_config(link_kind: LinkKind) -> Result<Library> {
    println!("Using pkg-config to find libturbojpeg");

    let mut cfg = pkg_config::Config::new();
    cfg.atleast_version("3.0");
    match link_kind {
        LinkKind::Static => { cfg.statik(true); },
        LinkKind::Dynamic => { cfg.statik(false); },
        LinkKind::Default => {},
    }

    let lib = cfg.probe("libturbojpeg")
        .context("could not find turbojpeg using pkg-config")?;

    Ok(Library {
        include_paths: lib.include_paths,
        defines: lib.defines,
    })
}

#[cfg(not(feature = "pkg-config"))]
fn find_pkg_config(_: LinkKind) -> Result<Library> {
    bail!("Trying to find turbojpeg using pkg-config, but the `pkg-config` feature is disabled. \
        You have two options:\n\
        - enable `pkg-config` feature of `turbojpeg-sys` crate\n\
        - use TURBOJPEG_SOURCE to select other source for the library")
}

fn find_explicit(link_kind: LinkKind) -> Result<Library> {
    println!("Using TURBOJPEG_LIB_DIR and TURBOJPEG_INCLUDE_DIR to find turbojpeg");

    let lib_dir = env_path("TURBOJPEG_LIB_DIR")
        .or_else(|| env_path("TURBOJPEG_LIB_PATH"))
        .context("TURBOJPEG_SOURCE is set to 'explicit', but TURBOJPEG_LIB_DIR is not set")?;
    let include_dir = env_path("TURBOJPEG_INCLUDE_DIR")
        .or_else(|| env_path("TURBOJPEG_INCLUDE_PATH"));

    let lib_dir = fs::canonicalize(lib_dir)
        .context("Cannot canonicalize TURBOJPEG_LIB_DIR")?;
    let include_dir = include_dir.map(fs::canonicalize).transpose()
        .context("Cannot canonicalize TURBOJPEG_INCLUDE_DIR")?;

    println!("cargo:rustc-link-search=native={}", lib_dir.display());
    println!("cargo:rustc-link-lib={}=turbojpeg", match link_kind {
        LinkKind::Static | LinkKind::Default => "static",
        LinkKind::Dynamic => "dylib",
    });

    Ok(Library {
        include_paths: include_dir.into_iter().collect(),
        defines: HashMap::new(),
    })
}

#[cfg(feature = "cmake")]
fn build_vendor(link_kind: LinkKind) -> Result<Library> {
    println!("Building turbojpeg from source");
    if !cfg!(feature = "require-simd") {
        check_nasm();
    }

    let source_path = PathBuf::from(env::var("CARGO_MANIFEST_DIR")?).join("libjpeg-turbo");
    let mut cmake = cmake::Config::new(source_path);
    cmake.configure_arg(format!("-DENABLE_SHARED={}", matches!(link_kind, LinkKind::Dynamic) as u32));
    cmake.configure_arg(format!("-DENABLE_STATIC={}", !matches!(link_kind, LinkKind::Dynamic) as u32));
    // Ignore DESTDIR because it will install turbojpeg to the wrong location
    cmake.env("DESTDIR", "");
    // On some 64 bit targets, the default libdir would be set to lib64.
    // Let's remain consistent across build targets and set the libdir ourselves,
    // instead of trying to figure out where to find the libs based on the target
    cmake.define("CMAKE_INSTALL_DEFAULT_LIBDIR", "lib");
    if cfg!(feature = "require-simd") {
        cmake.configure_arg("-DREQUIRE_SIMD=ON");
    }

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap();
    if target_os == "android" {
        let target_arch = env::var("CARGO_CFG_TARGET_ARCH").unwrap();
        let android_abi = match target_arch.as_str() {
            "arm" => "armeabi-v7a",
            "aarch64" => "arm64-v8a",
            "x86" => "x86",
            "x86_64" => "x86_64",
            _ => bail!("Unsupported Android arch: {target_arch:?}"),
        };
        cmake.configure_arg(format!("-DANDROID_ABI={android_abi}"));
    }

    let dst_path = cmake.build();

    let lib_path = dst_path.join("lib");
    let include_path = dst_path.join("include");

    let is_msvc = env("CARGO_CFG_TARGET_ENV").unwrap() == "msvc";

    println!("cargo:rustc-link-search=native={}", lib_path.display());
    println!("cargo:rustc-link-lib={}=turbojpeg{}", match link_kind {
        LinkKind::Static | LinkKind::Default => "static",
        LinkKind::Dynamic => "dylib",
    }, if is_msvc && matches!(link_kind, LinkKind::Static | LinkKind::Default) {
        "-static"
    } else {
        ""
    });

    Ok(Library {
        include_paths: vec![include_path],
        defines: HashMap::new(),
    })
}

fn check_nasm() {
    if !Command::new("nasm").arg("-v").status().map(|s| s.success()).unwrap_or(false) {
        println!("cargo:warning=NASM does not seem to be installed, so turbojpeg will be compiled without \
            SIMD extensions. Performance will suffer.");
    }
}

#[cfg(not(feature = "cmake"))]
fn build_vendor(_link_kind: LinkKind) -> Result<Library> {
    bail!("Trying to build turbojpeg from source, but the `cmake` feature is disabled.\
        You have two options:\n\
        - enable `cmake` feature of `turbojpeg-sys` crate\n\
        - use TURBOJPEG_SOURCE to select other source for the library")
}



fn generate_or_copy_bindings(library: &Library) -> Result<()> {
    match env("TURBOJPEG_BINDING") {
        Some(binding) => {
            if binding.eq_ignore_ascii_case("pregenerated") {
                copy_pregenerated_bindings()
            } else if binding.eq_ignore_ascii_case("bindgen") {
                generate_bindings(library)
            } else {
                bail!("Unknown value of TURBOJPEG_BINDING, supported values are:\n\
                    - `pregenerated` to use our pregenerated Rust bindings,\n\
                    - `bindgen` to generate the bindings with bindgen")
            }
        },
        None => {
            if cfg!(feature = "bindgen") {
                generate_bindings(library)
            } else {
                copy_pregenerated_bindings()
            }
        },
    }
}

fn copy_pregenerated_bindings() -> Result<()> {
    println!("Using pregenerated bindings");
    let out_path = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let crate_path = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    fs::copy(crate_path.join("bindings.rs"), out_path.join("bindings.rs"))?;
    println!("cargo:rerun-if-changed={}", crate_path.join("bindings.rs").to_str().unwrap());
    Ok(())
}

#[cfg(feature = "bindgen")]
fn generate_bindings(library: &Library) -> Result<()> {
    println!("Generating bindings using bindgen");

    let target = env::var("TARGET").unwrap();
    let mut builder = bindgen::Builder::default()
        .header("wrapper.h")
        .use_core()
        .ctypes_prefix("libc")
        .clang_args(&["-target", &target]);

    for path in library.include_paths.iter() {
        let path = path.to_str().unwrap();
        builder = builder.clang_arg(format!("-I{}", path));
        println!("cargo:rerun-if-changed={}", path);
    }

    for (name, value) in library.defines.iter() {
        if let Some(value) = value {
            builder = builder.clang_arg(format!("-D{}={}", name, value));
        } else {
            builder = builder.clang_arg(format!("-D{}", name));
        }
    }

    let bindings = builder.generate()
        .map_err(|_| anyhow!("could not generate bindings"))?;

    let out_file = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("bindings.rs");
    bindings.write_to_file(&out_file)
        .context("could not write bindings to OUT_DIR")?;
    println!("Generated bindings are stored in {}", out_file.display());

    Ok(())
}

#[cfg(not(feature = "bindgen"))]
fn generate_bindings(_: &Library) -> Result<()> {
    bail!("Trying to build bindings with bindgen, but the `bindgen` feature is disabled. \
        You have two options:\n\
        - enable `bindgen` feature of `turbojpeg-sys` crate\n\
        - use TURBOJPEG_BINDING to select other method to obtain the bindings")
}



fn env(name: &str) -> Option<OsString> {
    // adapted from `openssl-sys` crate

    fn env_inner(name: &str) -> Option<OsString> {
        let value = env::var_os(name);
        println!("cargo:rerun-if-env-changed={}", name);

        match value {
            Some(ref v) => println!("{} = {}", name, v.to_string_lossy()),
            None => println!("{} unset", name),
        }

        value
    }

    let prefix = env::var("TARGET").unwrap().to_uppercase().replace('-', "_");
    let prefixed = format!("{}_{}", prefix, name);
    env_inner(&prefixed).or_else(|| env_inner(name))
}

fn env_bool(name: &str) -> Result<Option<bool>> {
    match env(name) {
        Some(value) => {
            for v in ["", "1", "yes", "true", "on"].into_iter() {
                if value.eq_ignore_ascii_case(v) { return Ok(Some(true)) }
            }
            for v in ["0", "no", "false", "off"].into_iter() {
                if value.eq_ignore_ascii_case(v) { return Ok(Some(false)) }
            }
            bail!("Env variable {} has value {:?}, expected empty or boolean", name, value)
        },
        None => Ok(None),
    }
}

fn env_path(name: &str) -> Option<PathBuf> {
    env(name).map(|v| v.into())
}
