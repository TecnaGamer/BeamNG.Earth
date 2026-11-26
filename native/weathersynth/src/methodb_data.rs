//! The real thunder corpus's median 1/3-octave spectrum per class (dB re the 31.5 Hz-10 kHz total), what method B's
//! corpus_eq matches: exported from ~/bnge-tests/weather/compare (score.thunder_model()['med'], the screened CC0
//! corpus, 2026-11-25). Generated; do not edit. (band centre Hz, median dB); bands the model lacks are absent.

pub const CORPUS_STRIKE: &[(f64, f64)] = &[
    (f64::from_bits(0x403f400000000000), f64::from_bits(0xc03378bb18a0f9e0)), // 31.2 Hz -19.472
    (f64::from_bits(0x4043afaf27b421db), f64::from_bits(0xc03050aa72c5e56c)), // 39.4 Hz -16.315
    (f64::from_bits(0x4048cd9aad5dfda9), f64::from_bits(0xc0302fa43fbb9949)), // 49.6 Hz -16.186
    (f64::from_bits(0x404f400000000000), f64::from_bits(0xc02f13e9d5dfe696)), // 62.5 Hz -15.539
    (f64::from_bits(0x4053afaf27b421dc), f64::from_bits(0xc0292423d1d6fe86)), // 78.7 Hz -12.571
    (f64::from_bits(0x4058cd9aad5dfda7), f64::from_bits(0xc02701fd32cfea9e)), // 99.2 Hz -11.504
    (f64::from_bits(0x405f400000000000), f64::from_bits(0xc0241d7cf8603ee8)), // 125.0 Hz -10.058
    (f64::from_bits(0x4063afaf27b421dc), f64::from_bits(0xc025d3ab9397ab38)), // 157.5 Hz -10.913
    (f64::from_bits(0x4068cd9aad5dfda7), f64::from_bits(0xc0241bd156e6ca68)), // 198.4 Hz -10.054
    (f64::from_bits(0x406f400000000000), f64::from_bits(0xc0235e2d7b2e5553)), // 250.0 Hz -9.684
    (f64::from_bits(0x4073afaf27b421dc), f64::from_bits(0xc027a16ffaa9b38c)), // 315.0 Hz -11.815
    (f64::from_bits(0x4078cd9aad5dfda8), f64::from_bits(0xc0286305dcf48e82)), // 396.9 Hz -12.193
    (f64::from_bits(0x407f400000000000), f64::from_bits(0xc02ae0d4f0d1fbd4)), // 500.0 Hz -13.439
    (f64::from_bits(0x4083afaf27b421dc), f64::from_bits(0xc02d64bb3f3aa9c0)), // 630.0 Hz -14.697
    (f64::from_bits(0x4088cd9aad5dfda8), f64::from_bits(0xc030045b35248d44)), // 793.7 Hz -16.017
    (f64::from_bits(0x408f400000000000), f64::from_bits(0xc031dd87aa212c7c)), // 1000.0 Hz -17.865
    (f64::from_bits(0x4093afaf27b421dc), f64::from_bits(0xc0342300c54ac8be)), // 1259.9 Hz -20.137
    (f64::from_bits(0x4098cd9aad5dfda7), f64::from_bits(0xc03788a62a97af05)), // 1587.4 Hz -23.534
    (f64::from_bits(0x409f400000000000), f64::from_bits(0xc0391f7a463c0d70)), // 2000.0 Hz -25.123
    (f64::from_bits(0x40a3afaf27b421dc), f64::from_bits(0xc03d2ae3591dfc98)), // 2519.8 Hz -29.168
    (f64::from_bits(0x40a8cd9aad5dfda8), f64::from_bits(0xc0403858c8e77f80)), // 3174.8 Hz -32.440
    (f64::from_bits(0x40af400000000000), f64::from_bits(0xc042140cf6aec1e4)), // 4000.0 Hz -36.157
    (f64::from_bits(0x40b3afaf27b421dc), f64::from_bits(0xc0435248e87728cc)), // 5039.7 Hz -38.643
    (f64::from_bits(0x40b8cd9aad5dfda7), f64::from_bits(0xc0450bd9ba0e3cc2)), // 6349.6 Hz -42.093
    (f64::from_bits(0x40bf400000000000), f64::from_bits(0xc045a273bafbb038)), // 8000.0 Hz -43.269
];

pub const CORPUS_CLOSE: &[(f64, f64)] = &[
    (f64::from_bits(0x403f400000000000), f64::from_bits(0xc038cafed3b25aac)), // 31.2 Hz -24.793
    (f64::from_bits(0x4043afaf27b421db), f64::from_bits(0xc0366f206559f31e)), // 39.4 Hz -22.434
    (f64::from_bits(0x4048cd9aad5dfda9), f64::from_bits(0xc033a6472f50a38b)), // 49.6 Hz -19.650
    (f64::from_bits(0x404f400000000000), f64::from_bits(0xc032a423abfe0a72)), // 62.5 Hz -18.641
    (f64::from_bits(0x4053afaf27b421dc), f64::from_bits(0xc02ccc3faec80820)), // 78.7 Hz -14.399
    (f64::from_bits(0x4058cd9aad5dfda7), f64::from_bits(0xc02a6fc9f0358e4a)), // 99.2 Hz -13.218
    (f64::from_bits(0x405f400000000000), f64::from_bits(0xc024adcc2e0c3c7c)), // 125.0 Hz -10.339
    (f64::from_bits(0x4063afaf27b421dc), f64::from_bits(0xc0232623f0e09832)), // 157.5 Hz -9.574
    (f64::from_bits(0x4068cd9aad5dfda7), f64::from_bits(0xc021f0f2e174f06c)), // 198.4 Hz -8.971
    (f64::from_bits(0x406f400000000000), f64::from_bits(0xc02302d2999fb74a)), // 250.0 Hz -9.506
    (f64::from_bits(0x4073afaf27b421dc), f64::from_bits(0xc026cccdecc2c64f)), // 315.0 Hz -11.400
    (f64::from_bits(0x4078cd9aad5dfda8), f64::from_bits(0xc026b181c22cbbc0)), // 396.9 Hz -11.347
    (f64::from_bits(0x407f400000000000), f64::from_bits(0xc02a89320d0a7490)), // 500.0 Hz -13.268
    (f64::from_bits(0x4083afaf27b421dc), f64::from_bits(0xc02cff0b14d9f19c)), // 630.0 Hz -14.498
    (f64::from_bits(0x4088cd9aad5dfda8), f64::from_bits(0xc02e5b2268c97429)), // 793.7 Hz -15.178
    (f64::from_bits(0x408f400000000000), f64::from_bits(0xc031b1783494e55d)), // 1000.0 Hz -17.693
    (f64::from_bits(0x4093afaf27b421dc), f64::from_bits(0xc0350de2bd46b342)), // 1259.9 Hz -21.054
    (f64::from_bits(0x4098cd9aad5dfda7), f64::from_bits(0xc036ca7a7171393d)), // 1587.4 Hz -22.791
    (f64::from_bits(0x409f400000000000), f64::from_bits(0xc039d1fcf7ba5598)), // 2000.0 Hz -25.820
    (f64::from_bits(0x40a3afaf27b421dc), f64::from_bits(0xc03dd090e082331d)), // 2519.8 Hz -29.815
    (f64::from_bits(0x40a8cd9aad5dfda8), f64::from_bits(0xc04144f127609160)), // 3174.8 Hz -34.539
    (f64::from_bits(0x40af400000000000), f64::from_bits(0xc042ee8928cfc87c)), // 4000.0 Hz -37.864
    (f64::from_bits(0x40b3afaf27b421dc), f64::from_bits(0xc044e6e50c9923f6)), // 5039.7 Hz -41.804
    (f64::from_bits(0x40b8cd9aad5dfda7), f64::from_bits(0xc046e185c96ab080)), // 6349.6 Hz -45.762
    (f64::from_bits(0x40bf400000000000), f64::from_bits(0xc049405fd9be3906)), // 8000.0 Hz -50.503
];

pub const CORPUS_MID: &[(f64, f64)] = &[
    (f64::from_bits(0x403f400000000000), f64::from_bits(0xc0396fbb6a2436df)), // 31.2 Hz -25.436
    (f64::from_bits(0x4043afaf27b421db), f64::from_bits(0xc0347ddf3593d3ad)), // 39.4 Hz -20.492
    (f64::from_bits(0x4048cd9aad5dfda9), f64::from_bits(0xc03381dbf2cbe9f8)), // 49.6 Hz -19.507
    (f64::from_bits(0x404f400000000000), f64::from_bits(0xc03002ab974a77f9)), // 62.5 Hz -16.010
    (f64::from_bits(0x4053afaf27b421dc), f64::from_bits(0xc028124893d1d192)), // 78.7 Hz -12.036
    (f64::from_bits(0x4058cd9aad5dfda7), f64::from_bits(0xc02a295d84e8bbbc)), // 99.2 Hz -13.081
    (f64::from_bits(0x405f400000000000), f64::from_bits(0xc0254990bef3fadd)), // 125.0 Hz -10.644
    (f64::from_bits(0x4063afaf27b421dc), f64::from_bits(0xc024f0710635e30b)), // 157.5 Hz -10.470
    (f64::from_bits(0x4068cd9aad5dfda7), f64::from_bits(0xc02187a106e42edf)), // 198.4 Hz -8.765
    (f64::from_bits(0x406f400000000000), f64::from_bits(0xc02092b0dc64617f)), // 250.0 Hz -8.287
    (f64::from_bits(0x4073afaf27b421dc), f64::from_bits(0xc02715ab83cb5b6a)), // 315.0 Hz -11.542
    (f64::from_bits(0x4078cd9aad5dfda8), f64::from_bits(0xc02e876774ef8fa7)), // 396.9 Hz -15.264
    (f64::from_bits(0x407f400000000000), f64::from_bits(0xc030ad9e6b1db86b)), // 500.0 Hz -16.678
    (f64::from_bits(0x4083afaf27b421dc), f64::from_bits(0xc034b16ab2e7a352)), // 630.0 Hz -20.693
    (f64::from_bits(0x4088cd9aad5dfda8), f64::from_bits(0xc035672cd335c00a)), // 793.7 Hz -21.403
    (f64::from_bits(0x408f400000000000), f64::from_bits(0xc03988a05a29848e)), // 1000.0 Hz -25.534
    (f64::from_bits(0x4093afaf27b421dc), f64::from_bits(0xc03e2b404dac06aa)), // 1259.9 Hz -30.169
    (f64::from_bits(0x4098cd9aad5dfda7), f64::from_bits(0xc041f57ec910613a)), // 1587.4 Hz -35.918
    (f64::from_bits(0x409f400000000000), f64::from_bits(0xc044803d3058ef7a)), // 2000.0 Hz -41.002
    (f64::from_bits(0x40a3afaf27b421dc), f64::from_bits(0xc04674f27a8ba254)), // 2519.8 Hz -44.914
    (f64::from_bits(0x40a8cd9aad5dfda8), f64::from_bits(0xc047a4e59b1699a2)), // 3174.8 Hz -47.288
    (f64::from_bits(0x40af400000000000), f64::from_bits(0xc04798f52d01e848)), // 4000.0 Hz -47.195
    (f64::from_bits(0x40b3afaf27b421dc), f64::from_bits(0xc04aafdbc132f01e)), // 5039.7 Hz -53.374
    (f64::from_bits(0x40b8cd9aad5dfda7), f64::from_bits(0xc04b02a67d666bfc)), // 6349.6 Hz -54.021
    (f64::from_bits(0x40bf400000000000), f64::from_bits(0xc04aea200386894e)), // 8000.0 Hz -53.829
];

pub const CORPUS_FAR: &[(f64, f64)] = &[
    (f64::from_bits(0x403f400000000000), f64::from_bits(0xc0351bf19c4d0103)), // 31.2 Hz -21.109
    (f64::from_bits(0x4043afaf27b421db), f64::from_bits(0xc0302f7dbb771896)), // 39.4 Hz -16.186
    (f64::from_bits(0x4048cd9aad5dfda9), f64::from_bits(0xc029d0387e1f4e37)), // 49.6 Hz -12.907
    (f64::from_bits(0x404f400000000000), f64::from_bits(0xc029f94afbbb7fd0)), // 62.5 Hz -12.987
    (f64::from_bits(0x4053afaf27b421dc), f64::from_bits(0xc0266034623fc270)), // 78.7 Hz -11.188
    (f64::from_bits(0x4058cd9aad5dfda7), f64::from_bits(0xc02448d439147e18)), // 99.2 Hz -10.142
    (f64::from_bits(0x405f400000000000), f64::from_bits(0xc021daaf8003a26a)), // 125.0 Hz -8.927
    (f64::from_bits(0x4063afaf27b421dc), f64::from_bits(0xc02302b3f3b00632)), // 157.5 Hz -9.505
    (f64::from_bits(0x4068cd9aad5dfda7), f64::from_bits(0xc027866a765c7bd7)), // 198.4 Hz -11.763
    (f64::from_bits(0x406f400000000000), f64::from_bits(0xc0275d84830a9f96)), // 250.0 Hz -11.683
    (f64::from_bits(0x4073afaf27b421dc), f64::from_bits(0xc02cd141eeb1bb5f)), // 315.0 Hz -14.409
    (f64::from_bits(0x4078cd9aad5dfda8), f64::from_bits(0xc03219f561839768)), // 396.9 Hz -18.101
    (f64::from_bits(0x407f400000000000), f64::from_bits(0xc0369b4482e72303)), // 500.0 Hz -22.607
    (f64::from_bits(0x4083afaf27b421dc), f64::from_bits(0xc039e42263a39206)), // 630.0 Hz -25.891
    (f64::from_bits(0x4088cd9aad5dfda8), f64::from_bits(0xc03e3d4248a5116e)), // 793.7 Hz -30.239
    (f64::from_bits(0x408f400000000000), f64::from_bits(0xc04292997375373e)), // 1000.0 Hz -37.145
    (f64::from_bits(0x4093afaf27b421dc), f64::from_bits(0xc045150347756632)), // 1259.9 Hz -42.164
    (f64::from_bits(0x4098cd9aad5dfda7), f64::from_bits(0xc04739fdca8cab23)), // 1587.4 Hz -46.453
    (f64::from_bits(0x409f400000000000), f64::from_bits(0xc049f180d0156728)), // 2000.0 Hz -51.887
    (f64::from_bits(0x40a3afaf27b421dc), f64::from_bits(0xc04b02f0fe86c60c)), // 2519.8 Hz -54.023
    (f64::from_bits(0x40a8cd9aad5dfda8), f64::from_bits(0xc04cb5af78fe53ff)), // 3174.8 Hz -57.419
    (f64::from_bits(0x40af400000000000), f64::from_bits(0xc04c5bbc2f6686d2)), // 4000.0 Hz -56.717
    (f64::from_bits(0x40b3afaf27b421dc), f64::from_bits(0xc04e000000000000)), // 5039.7 Hz -60.000
    (f64::from_bits(0x40b8cd9aad5dfda7), f64::from_bits(0xc04d5fda1b84437c)), // 6349.6 Hz -58.749
    (f64::from_bits(0x40bf400000000000), f64::from_bits(0xc04d8253d0b9f82a)), // 8000.0 Hz -59.018
];
