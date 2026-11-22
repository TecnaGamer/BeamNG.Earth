# turbojpeg-sys

Raw Rust bindings for the [`turbojpeg`][libjpeg-turbo] library. If you want to
work with JPEG files in Rust, you should use the high-level bindings from the
`turbojpeg` crate.

[libjpeg-turbo]: https://libjpeg-turbo.org/

These bindings are for TurboJPEG version 3.0 and use the new API (prefixed with
`tj3`). Note that most package managers for Linux have only TurboJPEG 2.0 or
2.1.

## Building

We support multiple options for building the native TurboJPEG library and
linking to it. There are three aspects that you can control:

- **Source:** should we build the library ourselves, or should we use a compiled
    version from your system?
- **Linking** should we use static or dynamic linking?
- **Binding:** should we use pregenerated Rust bindings, or should we generate
    them at build time?

### Source

TurboJPEG is written in C, so we must either compile it ourselves, or look up a
compiled library on your system. You can control what we do using
`TURBOJPEG_SOURCE` environment variable:

- `TURBOJPEG_SOURCE=vendor` (**recommended**, default if the `cmake` feature is
    enabled): we build TurboJPEG from source using the [`cmake`][cmake-crate]
    crate and link it to your Rust executable. We use TurboJPEG sources that are
    bundled with the crate (version 3.1.0).

    This option requires a C compiler, and if you want to compile the SIMD code
    that actually makes TurboJPEG fast, you will also [need NASM or
    Yasm][turbojpeg-building]. By default, if TurboJPEG does not find NASM, you
    will receive a compilation error. However, you can disable the default
    feature `require-simd` and TurboJPEG will just skip the SIMD code when NASM
    is not found (but performance will suffer).

- `TURBOJPEG_SOURCE=pkg-config` (default if the `cmake` feature is disabled and
    `pkg-config` is enabled): we look up the library using
    [`pkg-config`][pkgconf-crate].

- `TURBOJPEG_SOURCE=explicit` (default if `cmake` and `pkg-config` features are
    disabled): we look up the library in `TURBOJPEG_LIB_DIR`. If you want to
    generate the bindings at build time (see below), then you should also set
    `TURBOJPEG_INCLUDE_DIR` to point to the directory with the `turbojpeg.h`
    header.

[cmake-crate]: https://docs.rs/cmake/latest/cmake/
[pkgconf-crate]: https://docs.rs/pkg-config/latest/pkg_config/
[turbojpeg-building]: https://github.com/libjpeg-turbo/libjpeg-turbo/blob/main/BUILDING.md

### Linking

We can link the compiled library from the previous step to your Rust executable
either statically (the library becomes part of the executable) or dynamically
(the library is looked up at runtime). You can control this using environment
variables:

- `TURBOJPEG_STATIC=1` configures static linking.
- `TURBOJPEG_DYNAMIC=1` (or `TURBOJPEG_SHARED=1`) configures dynamic linking.

If you don't specify any of these variables, the default behavior depends on
`TURBOJPEG_SOURCE`. If `TURBOJPEG_SOURCE` is `vendor` or `explicit`, we link
statically by default. However, if you use `pkg-config`, we [let the
`pkg-config` crate decide][pkgconf-crate]; it typically uses dynamic linking by
default.

### Binding

To use the C library in Rust, we need some boilerplate "binding" code that
exports C symbols (functions and variables) into Rust. We have two options and
you control the decision with the `TURBOJPEG_BINDING` environment variable:

- `TURBOJPEG_BINDING=pregenerated` (default unless the `bindgen` feature is
    enabled): use a binding code that is shipped with this crate. This should
    work most of the time and it is the recommended option.

- `TURBOJPEG_BINDING=bindgen` (default if the `bindgen` feature is enabled):
    generate the binding during build using [bindgen][bindgen-crate]. This is
    normally not necessary, but it might save your day if your TurboJPEG is
    somehow incompatible with our pregenerated binding code.

[bindgen-crate]: https://docs.rs/bindgen/latest/bindgen/

## Features

This crate supports multiple features:

- `cmake` (default): allows us to build TurboJPEG from source
    (`TURBOJPEG_SOURCE=vendor`).
- `require-simd` (default): when building TurboJPEG from source, aborts the
    compilation when NASM is not found and the fast SIMD code would be skipped.
- `pkg-config` (default): allows us to find TurboJPEG using `pkg-config`
    (`TURBOJPEG_SOURCE=pkg-config`).
- `bindgen`: allows us to generate the bindings at build time using `bindgen`.

Note that the `turbojpeg` crate "reexports" these features.

## Cargo metadata

The build script of this crate sets some [metadata][cargo-metadata], and build
scripts of direct dependencies can access it from environment variables:

- `DEP_TURBOJPEG_INCLUDE`: include paths for the C/C++ headers of TurboJPEG.
  Multiple paths are separated with a comma (`,`).

[cargo-metadata]: https://doc.rust-lang.org/cargo/reference/build-scripts.html#the-links-manifest-key
