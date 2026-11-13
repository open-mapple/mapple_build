/* SHA-1 (the boot image header's id) and SHA-256 (read-back checks), FIPS 180-4. */
#ifndef SHA_H
#define SHA_H
#include <stdint.h>
#include <string.h>

#define ROL(x, n) ((x) << (n) | (x) >> (32 - (n)))
#define ROR(x, n) ((x) >> (n) | (x) << (32 - (n)))

struct sha1 { uint32_t h[5]; uint64_t n; uint8_t buf[64]; };
struct sha256 { uint32_t h[8]; uint64_t n; uint8_t buf[64]; };

static inline uint32_t be32(const uint8_t *p) { return (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]; }

static inline void sha1_block(struct sha1 *s, const uint8_t *p)
{
	uint32_t w[80], a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4], f, k, t;

	for (int i = 0; i < 16; i++)
		w[i] = be32(p + 4 * i);
	for (int i = 16; i < 80; i++)
		w[i] = ROL(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
	for (int i = 0; i < 80; i++) {
		if (i < 20)      { f = (b & c) | (~b & d);          k = 0x5a827999; }
		else if (i < 40) { f = b ^ c ^ d;                   k = 0x6ed9eba1; }
		else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }
		else             { f = b ^ c ^ d;                   k = 0xca62c1d6; }
		t = ROL(a, 5) + f + e + k + w[i];
		e = d; d = c; c = ROL(b, 30); b = a; a = t;
	}
	s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e;
}

static const uint32_t sha256_k[64] = {
	0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
	0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
	0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
	0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
	0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
	0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
	0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
	0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static inline void sha256_block(struct sha256 *s, const uint8_t *p)
{
	uint32_t w[64], v[8], t1, t2;

	for (int i = 0; i < 16; i++)
		w[i] = be32(p + 4 * i);
	for (int i = 16; i < 64; i++)
		w[i] = w[i - 16] + (ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ w[i - 15] >> 3) + w[i - 7] +
		       (ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ w[i - 2] >> 10);
	memcpy(v, s->h, sizeof(v));
	for (int i = 0; i < 64; i++) {
		t1 = v[7] + (ROR(v[4], 6) ^ ROR(v[4], 11) ^ ROR(v[4], 25)) + ((v[4] & v[5]) ^ (~v[4] & v[6])) +
		     sha256_k[i] + w[i];
		t2 = (ROR(v[0], 2) ^ ROR(v[0], 13) ^ ROR(v[0], 22)) + ((v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]));
		memmove(v + 1, v, 7 * sizeof(v[0]));
		v[4] += t1;
		v[0] = t1 + t2;
	}
	for (int i = 0; i < 8; i++)
		s->h[i] += v[i];
}

/* both hashes share the padding: 64-byte blocks, a 0x80 byte, the length in bits big-endian */
#define SHA_UPDATE(s, block, data, len) do {					\
	const uint8_t *p_ = (const uint8_t *)(data);				\
	size_t len_ = (len);							\
	while (len_) {								\
		size_t have_ = (size_t)((s)->n % 64), take_ = 64 - have_;	\
		if (take_ > len_) take_ = len_;					\
		if (!have_ && len_ >= 64) { block(s, p_); take_ = 64; }		\
		else { memcpy((s)->buf + have_, p_, take_);			\
		       if (have_ + take_ == 64) block(s, (s)->buf); }		\
		(s)->n += take_; p_ += take_; len_ -= take_;			\
	}									\
} while (0)

#define SHA_FINAL(s, block, update, words, out) do {				\
	uint8_t pad_[72] = { 0x80 }, len_[8];					\
	uint64_t bits_ = (s)->n * 8;						\
	for (int i_ = 0; i_ < 8; i_++) len_[i_] = (uint8_t)(bits_ >> (56 - 8 * i_)); \
	update(s, pad_, 1 + (119 - (s)->n % 64) % 64);				\
	update(s, len_, 8);							\
	for (int i_ = 0; i_ < (words); i_++)					\
		for (int j_ = 0; j_ < 4; j_++)					\
			(out)[4 * i_ + j_] = (uint8_t)((s)->h[i_] >> (24 - 8 * j_)); \
} while (0)

static inline void sha1_init(struct sha1 *s)
{
	static const uint32_t h[5] = { 0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0 };
	memcpy(s->h, h, sizeof(h)); s->n = 0;
}
static inline void sha1_update(struct sha1 *s, const void *d, size_t n) { SHA_UPDATE(s, sha1_block, d, n); }
static inline void sha1_final(struct sha1 *s, uint8_t out[20]) { SHA_FINAL(s, sha1_block, sha1_update, 5, out); }

static inline void sha256_init(struct sha256 *s)
{
	static const uint32_t h[8] = { 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
				       0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19 };
	memcpy(s->h, h, sizeof(h)); s->n = 0;
}
static inline void sha256_update(struct sha256 *s, const void *d, size_t n) { SHA_UPDATE(s, sha256_block, d, n); }
static inline void sha256_final(struct sha256 *s, uint8_t out[32]) { SHA_FINAL(s, sha256_block, sha256_update, 8, out); }

#endif
