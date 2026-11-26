// Generated from numpy 2.5.4 (the expected draws); do not edit. lognormal within 1e-15: its exp is the libm crate's
// here, glibc's in numpy (1 ulp apart at times); every other draw is bit for bit.
#[test]
fn matches_numpy() {
    let mut g = NpRng::new(&[12]);
    assert_eq!(g.random().to_bits(), 0x3fd00d8207273672, "random [12]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a6d0424296f6f4, "uniform [12]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fe7bb17a410d77b, "standard_normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc462f947b65a66, "normal [12]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x404c14e7c88dc22f); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 230541);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.random().to_bits(), 0x3fe153b0d80f21bd, "random [12]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x4099f11b8e910762, "uniform [12]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fc2d2ffa36e332a, "standard_normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfc98a1e1c622ead, "normal [12]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x404e5e78bf6869f7); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 468147);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.random().to_bits(), 0x3fed87aaac4d91c0, "random [12]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a608bffffc25f7, "uniform [12]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfe35fdce46ea4b0, "standard_normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfc0bc91afb32464, "normal [12]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4035b282b0eaac94); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 871558);
    assert_eq!(g.poisson_small(3.5), 5);
    assert_eq!(g.random().to_bits(), 0x3fc132700eedbe9c, "random [12]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a4aae0119f9c9a, "uniform [12]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fc8819fe9650d5e, "standard_normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfbe0fea43c707e8, "normal [12]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403a3329c3426ace); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 600112);
    assert_eq!(g.poisson_small(3.5), 2);
    assert_eq!(g.random().to_bits(), 0x3fe8f8e55a3d45a5, "random [12]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a66f92e20d1800, "uniform [12]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fe39ba07f013ae9, "standard_normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfb6065aae18b3a8, "normal [12]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x40230be24e15e314); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 536090);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fd7a45713071cdf, "normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fdf237e484261ff, "normal [12]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fd4b7ba0417c4df, "normal [12]");
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    let mut g = NpRng::new(&[0]);
    assert_eq!(g.random().to_bits(), 0x3fe461fd79fb3850, "random [0]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x409dc2b864707ad1, "uniform [0]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fe47e57a468b06d, "standard_normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3f97a1c417d10d4e, "normal [0]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4033203819bf8f99); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 912758);
    assert_eq!(g.poisson_small(3.5), 5);
    assert_eq!(g.random().to_bits(), 0x3feb6fdb17ec9cde, "random [0]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x4098398371944de3, "uniform [0]");
    assert_eq!(g.standard_normal().to_bits(), 0xbff3ef405142e27e, "standard_normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfc49ee2d571180d, "normal [0]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x40330b3c65d500e0); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 541462);
    assert_eq!(g.poisson_small(3.5), 2);
    assert_eq!(g.random().to_bits(), 0x3fbfd10760015ec8, "random [0]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a393df8cec90da, "uniform [0]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fd67f2417d0e872, "standard_normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc97114952441a6, "normal [0]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403a3405a8a92737); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 997212);
    assert_eq!(g.poisson_small(3.5), 5);
    assert_eq!(g.random().to_bits(), 0x3fe7166eb7328660, "random [0]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a1e0101ea22071, "uniform [0]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fd6be6d2ccde9a7, "standard_normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfc2696c9e6916a5, "normal [0]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x40376e63f3fc5390); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 934046);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.random().to_bits(), 0x3fd9104923f0aea4, "random [0]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a626d2b3cc8886, "uniform [0]");
    assert_eq!(g.standard_normal().to_bits(), 0x3ff7540d7f0aecc3, "standard_normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fdb99b63ed8f94c, "normal [0]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x404ec52a98e7a81e); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 832646);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbfd8bc8de46ad5f0, "normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fbe586a71ec4728, "normal [0]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fc081bb68a65e8f, "normal [0]");
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 1);
    let mut g = NpRng::new(&[11, 7777, 3]);
    assert_eq!(g.random().to_bits(), 0x3fd9562f17705c28, "random [11, 7777, 3]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a66001712fe718, "uniform [11, 7777, 3]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fe9b2e48235cae4, "standard_normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfb1735f3f092ab5, "normal [11, 7777, 3]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403b64814f74a1d9); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 746964);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.random().to_bits(), 0x3fc04c4c461f2890, "random [11, 7777, 3]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a12f84c1de3db6, "uniform [11, 7777, 3]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fd7e85fabed9cb7, "standard_normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfafbf3069a52ef7, "normal [11, 7777, 3]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4037268e18479e62); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 683011);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.random().to_bits(), 0x3feee521ce06925f, "random [11, 7777, 3]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a752c67a37d7b8, "uniform [11, 7777, 3]");
    assert_eq!(g.standard_normal().to_bits(), 0xbff517555aed4cc6, "standard_normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfd0969f6f26c1f2, "normal [11, 7777, 3]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x40432e27aa199bfa); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 257969);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.random().to_bits(), 0x3fe1d8c5e5d896c0, "random [11, 7777, 3]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x4098910d0a27fb07, "uniform [11, 7777, 3]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fd7c3611fd36a55, "standard_normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fca9b85a9d4ca11, "normal [11, 7777, 3]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403932f05b7b5284); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 106708);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.random().to_bits(), 0x3fd7db74652175d8, "random [11, 7777, 3]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x409908655e446a16, "uniform [11, 7777, 3]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfe1146fc389d28d, "standard_normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc67e93f7666422, "normal [11, 7777, 3]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4025304ec3d50cf1); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 132644);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fe022f2e879a711, "normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fc1ca8b135b06df, "normal [11, 7777, 3]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbfc092cd9ae0ebf2, "normal [11, 7777, 3]");
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    let mut g = NpRng::new(&[41, 8888, 2, 17]);
    assert_eq!(g.random().to_bits(), 0x3fc139970dab1400, "random [41, 8888, 2, 17]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a19ff0b2dec1d4, "uniform [41, 8888, 2, 17]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfe6f7f833460935, "standard_normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fd4b055f3e98fc9, "normal [41, 8888, 2, 17]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403dc409bfe11a51); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 107388);
    assert_eq!(g.poisson_small(3.5), 6);
    assert_eq!(g.random().to_bits(), 0x3fcaf6c68b5dc268, "random [41, 8888, 2, 17]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40994d9d0a13fc04, "uniform [41, 8888, 2, 17]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fef32d882deaeac, "standard_normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc20c8421089ac1, "normal [41, 8888, 2, 17]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403abf58c0dbb2e1); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 454589);
    assert_eq!(g.poisson_small(3.5), 5);
    assert_eq!(g.random().to_bits(), 0x3fec387524cd96e0, "random [41, 8888, 2, 17]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a3694669f959b2, "uniform [41, 8888, 2, 17]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfd40fafaea2d8cb, "standard_normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfd79cc3f38ed2e8, "normal [41, 8888, 2, 17]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403acd62c50117d1); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 663588);
    assert_eq!(g.poisson_small(3.5), 1);
    assert_eq!(g.random().to_bits(), 0x3fef88d06f8547e5, "random [41, 8888, 2, 17]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x409996b2850fdff0, "uniform [41, 8888, 2, 17]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfbb210fe4624588, "standard_normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfc12288d2e337b8, "normal [41, 8888, 2, 17]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403036991a9eb67e); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 682580);
    assert_eq!(g.poisson_small(3.5), 1);
    assert_eq!(g.random().to_bits(), 0x3fe810de5eddc990, "random [41, 8888, 2, 17]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x4099d965c0b53172, "uniform [41, 8888, 2, 17]");
    assert_eq!(g.standard_normal().to_bits(), 0x4003d09acfd16290, "standard_normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc8614f7d2e4131, "normal [41, 8888, 2, 17]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4043409625170a1f); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 387720);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fe0c07c1fcf9422, "normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbfcc658f479c529b, "normal [41, 8888, 2, 17]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fbcbb2eb568a014, "normal [41, 8888, 2, 17]");
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 0);
    let mut g = NpRng::new(&[21, 5151, 30000, 412]);
    assert_eq!(g.random().to_bits(), 0x3fdea19cf31dd120, "random [21, 5151, 30000, 412]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a333f431db3bc3, "uniform [21, 5151, 30000, 412]");
    assert_eq!(g.standard_normal().to_bits(), 0x40041864e7c4fbdb, "standard_normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc44fba838905d4, "normal [21, 5151, 30000, 412]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403d56782af4c870); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 757292);
    assert_eq!(g.poisson_small(3.5), 6);
    assert_eq!(g.random().to_bits(), 0x3fa87733ca374110, "random [21, 5151, 30000, 412]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x409b46e27d19b872, "uniform [21, 5151, 30000, 412]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fe443b545d4cf87, "standard_normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc88f2983ce3b35, "normal [21, 5151, 30000, 412]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4035f4209cc03e40); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 45687);
    assert_eq!(g.poisson_small(3.5), 4);
    assert_eq!(g.random().to_bits(), 0x3fd6ac78448060c2, "random [21, 5151, 30000, 412]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a674590504c66a, "uniform [21, 5151, 30000, 412]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfff694bbf332f8a, "standard_normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfc64258326186b8, "normal [21, 5151, 30000, 412]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x404a30059fddb927); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 773772);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.random().to_bits(), 0x3fdc7378af8c76de, "random [21, 5151, 30000, 412]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a0e9713b59b695, "uniform [21, 5151, 30000, 412]");
    assert_eq!(g.standard_normal().to_bits(), 0x3ff6f9c0c4b9e364, "standard_normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fb150c308978440, "normal [21, 5151, 30000, 412]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403102253f04bae4); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 588032);
    assert_eq!(g.poisson_small(3.5), 5);
    assert_eq!(g.random().to_bits(), 0x3fec7fcf947adc64, "random [21, 5151, 30000, 412]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a6c475255c53b8, "uniform [21, 5151, 30000, 412]");
    assert_eq!(g.standard_normal().to_bits(), 0x3ff7e84c44b1161a, "standard_normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fd34a32acb15428, "normal [21, 5151, 30000, 412]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4039a385994b40d3); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 438123);
    assert_eq!(g.poisson_small(3.5), 3);
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbfd11b8337678374, "normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbfbdf5d357553dba, "normal [21, 5151, 30000, 412]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbfb5fc127141c7a3, "normal [21, 5151, 30000, 412]");
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 0);
    let mut g = NpRng::new(&[8589934599]);
    assert_eq!(g.random().to_bits(), 0x3fd1ca43ddd2012c, "random [8589934599]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a0ef759965a292, "uniform [8589934599]");
    assert_eq!(g.standard_normal().to_bits(), 0xbff3a152f91a796e, "standard_normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc2c78d113e9b94, "normal [8589934599]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x404a2ed5641d11ae); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 136905);
    assert_eq!(g.poisson_small(3.5), 1);
    assert_eq!(g.random().to_bits(), 0x3fe8e9085867822b, "random [8589934599]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40987393f301da3e, "uniform [8589934599]");
    assert_eq!(g.standard_normal().to_bits(), 0xbfe953e33b6c1c64, "standard_normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0xbfbdcc897886c646, "normal [8589934599]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x4033493ed78bd909); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 2);
    assert_eq!(g.integers(0, 1000003), 13556);
    assert_eq!(g.poisson_small(3.5), 6);
    assert_eq!(g.random().to_bits(), 0x3fe3e8f61bb53611, "random [8589934599]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x40a317de9122ea60, "uniform [8589934599]");
    assert_eq!(g.standard_normal().to_bits(), 0xc0000e6045723a75, "standard_normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fc98cabd3d51cb1, "normal [8589934599]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x405b85c6a4196e7f); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 317544);
    assert_eq!(g.poisson_small(3.5), 2);
    assert_eq!(g.random().to_bits(), 0x3fed4bd3e6e5ac1b, "random [8589934599]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x409e0ae21b2d969a, "uniform [8589934599]");
    assert_eq!(g.standard_normal().to_bits(), 0x3ff42a4d291a69c8, "standard_normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fce96e30a7fb615, "normal [8589934599]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x403fddbe36c75574); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 3);
    assert_eq!(g.integers(0, 1000003), 102982);
    assert_eq!(g.poisson_small(3.5), 1);
    assert_eq!(g.random().to_bits(), 0x3fc897cfa6da13c4, "random [8589934599]");
    assert_eq!(g.uniform(1500.0_f64, 3000.0_f64).to_bits(), 0x409b3d68bb7b4a93, "uniform [8589934599]");
    assert_eq!(g.standard_normal().to_bits(), 0x3fdee73119f8e1de, "standard_normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.22_f64).to_bits(), 0x3fac04c82a7793f7, "normal [8589934599]");
    { let v = g.lognormal(f64::from_bits(0x4009c041f7ed8d33), 0.5); let w = f64::from_bits(0x402b8a9c67e76008); assert!((v - w).abs() <= w.abs() * 1e-15, "lognormal {v} {w}"); }
    assert_eq!(g.integers(2, 5), 4);
    assert_eq!(g.integers(0, 1000003), 300709);
    assert_eq!(g.poisson_small(3.5), 5);
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fdc3dea22ba969d, "normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0xbf909d383bade4ab, "normal [8589934599]");
    assert_eq!(g.normal(0.0_f64, 0.3_f64).to_bits(), 0x3fc75d76157d7ed0, "normal [8589934599]");
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 1);
    assert_eq!(g.choice_index(2), 0);
    assert_eq!(g.choice_index(2), 1);
}
