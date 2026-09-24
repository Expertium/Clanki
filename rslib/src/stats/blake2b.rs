// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! BLAKE2b (RFC 7693) without a key, as Python's `hashlib.blake2b` computes
//! it. Total Knowledge keeps its day sums under a BLAKE2b digest of the
//! history (`qt/aqt/total_knowledge.py`), and computes that digest here now;
//! a digest that differed would throw every kept sum away.

const IV: [u64; 8] = [
    0x6a09e667f3bcc908,
    0xbb67ae8584caa73b,
    0x3c6ef372fe94f82b,
    0xa54ff53a5f1d36f1,
    0x510e527fade682d1,
    0x9b05688c2b3e6c1f,
    0x1f83d9abfb41bd6b,
    0x5be0cd19137e2179,
];

const SIGMA: [[usize; 16]; 10] = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
    [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
    [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
    [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
    [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
    [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
    [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
    [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
    [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
];

const BLOCK: usize = 128;

pub(crate) struct Blake2b {
    h: [u64; 8],
    /// bytes compressed so far
    counter: u128,
    buffer: [u8; BLOCK],
    buffered: usize,
    digest_size: usize,
}

impl Blake2b {
    /// `digest_size` in bytes, 1 to 64.
    pub(crate) fn new(digest_size: usize) -> Self {
        assert!((1..=64).contains(&digest_size));
        let mut h = IV;
        h[0] ^= 0x0101_0000 ^ digest_size as u64;
        Self {
            h,
            counter: 0,
            buffer: [0; BLOCK],
            buffered: 0,
            digest_size,
        }
    }

    pub(crate) fn update(&mut self, mut data: &[u8]) {
        while !data.is_empty() {
            // the last block is compressed by `hex_digest`, with the final flag,
            // so a full buffer waits until more data comes
            if self.buffered == BLOCK {
                self.counter += BLOCK as u128;
                compress(&mut self.h, &self.buffer, self.counter, false);
                self.buffered = 0;
            }
            let take = (BLOCK - self.buffered).min(data.len());
            self.buffer[self.buffered..self.buffered + take].copy_from_slice(&data[..take]);
            self.buffered += take;
            data = &data[take..];
        }
    }

    pub(crate) fn hex_digest(mut self) -> String {
        use std::fmt::Write;

        self.counter += self.buffered as u128;
        self.buffer[self.buffered..].fill(0);
        compress(&mut self.h, &self.buffer, self.counter, true);
        self.h
            .iter()
            .flat_map(|word| word.to_le_bytes())
            .take(self.digest_size)
            .fold(
                String::with_capacity(self.digest_size * 2),
                |mut out, byte| {
                    write!(out, "{byte:02x}").unwrap();
                    out
                },
            )
    }
}

fn compress(h: &mut [u64; 8], block: &[u8; BLOCK], counter: u128, last: bool) {
    let mut m = [0u64; 16];
    for (word, bytes) in m.iter_mut().zip(block.chunks_exact(8)) {
        *word = u64::from_le_bytes(bytes.try_into().unwrap());
    }
    let mut v = [0u64; 16];
    v[..8].copy_from_slice(h);
    v[8..].copy_from_slice(&IV);
    v[12] ^= counter as u64;
    v[13] ^= (counter >> 64) as u64;
    if last {
        v[14] = !v[14];
    }
    for round in 0..12 {
        let s = &SIGMA[round % 10];
        mix(&mut v, 0, 4, 8, 12, m[s[0]], m[s[1]]);
        mix(&mut v, 1, 5, 9, 13, m[s[2]], m[s[3]]);
        mix(&mut v, 2, 6, 10, 14, m[s[4]], m[s[5]]);
        mix(&mut v, 3, 7, 11, 15, m[s[6]], m[s[7]]);
        mix(&mut v, 0, 5, 10, 15, m[s[8]], m[s[9]]);
        mix(&mut v, 1, 6, 11, 12, m[s[10]], m[s[11]]);
        mix(&mut v, 2, 7, 8, 13, m[s[12]], m[s[13]]);
        mix(&mut v, 3, 4, 9, 14, m[s[14]], m[s[15]]);
    }
    for i in 0..8 {
        h[i] ^= v[i] ^ v[i + 8];
    }
}

fn mix(v: &mut [u64; 16], a: usize, b: usize, c: usize, d: usize, x: u64, y: u64) {
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(x);
    v[d] = (v[d] ^ v[a]).rotate_right(32);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(24);
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(y);
    v[d] = (v[d] ^ v[a]).rotate_right(16);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(63);
}

#[cfg(test)]
mod test {
    use super::*;

    /// The digests Python's `hashlib.blake2b(data, digest_size=20)` gives
    /// for `data[i] = (7 * i + 3) % 256`, across the block edges; and the
    /// RFC's "abc".
    #[test]
    fn digests_match_python() {
        for (length, expected) in [
            (0, "3345524abf6bbe1809449224b5972c41790b6cf2"),
            (1, "469faac899f374872debf8316818e61309b0f769"),
            (3, "8f148bd4c242f537b55ee7b7ba68153d17c49871"),
            (127, "23ff7f1d4b5e5568040f01adbe04a6b997899d25"),
            (128, "79ae684511c83fdee5f040c77bb7c9a317d3f2de"),
            (129, "b195dc7be88c88574f88d235228691bf35e409ec"),
            (256, "4514db2f29d22e32d1a142894c7285e546dbf121"),
            (1000, "7347a87d12c5b902eefd08b8b5c14816494c2639"),
        ] {
            let data: Vec<u8> = (0..length).map(|i| ((i * 7 + 3) % 256) as u8).collect();
            let mut whole = Blake2b::new(20);
            whole.update(&data);
            assert_eq!(whole.hex_digest(), expected, "length {length}");
            // fed in uneven pieces, the digest is the same
            let mut pieces = Blake2b::new(20);
            for piece in data.chunks(37) {
                pieces.update(piece);
            }
            assert_eq!(pieces.hex_digest(), expected, "length {length} in pieces");
        }
        let mut abc = Blake2b::new(64);
        abc.update(b"abc");
        assert_eq!(
            abc.hex_digest(),
            "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1\
             7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"
        );
    }
}
