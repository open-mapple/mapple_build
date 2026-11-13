/*
 * mapple-install: what the install zip runs in the recovery (static aarch64 Linux), and what mka uses on the Mac for
 * the same boot image.
 *
 *   mapple-install verify-root RANGES DEV
 *	DEV holds the root RANGES describes (rootranges.py): both volume headers, then every allocated block's SHA-256
 *   mapple-install put DEV REGION|OFFSET FILE
 *	FILE written into a region of DEV's tail (settings, firmware, log; zero-filled to its end) or at a byte offset;
 *	read back and hashed
 *   mapple-install save DEV NAME FILE
 *	FILE kept in DEV's saved area under NAME (boot), its header updated; read back and hashed. NAME chained
 *	records only FILE's size and SHA-256 (the boot image as we chained it)
 *   mapple-install ilifw FIRMWARE OUT
 *	the touch firmware (FW_NF_ILI7807S_*.bin, from the phone's odm) as mtkloader's item ilifw
 *   mapple-install chain BOOT LOADER PAYLOAD [--backup FILE] [--out FILE]
 *	the loader put in front of the Android kernel of BOOT (a boot partition, or a copy of one) and the payload
 *	after it; written to --out, or back to BOOT, then read back and hashed. --backup keeps BOOT as it was, if it
 *	carries no loader yet. A kernel that already carries a loader is chained again from the Android kernel inside it.
 *
 * DEV is the partition that holds the root (userdata in single boot). It ends in two areas of 64 MiB:
 *   end - 128 MiB  saved: a 4 KiB header, then a slot per item (boot at +1 MiB)
 *   end -  64 MiB  tail: settings record 1 MiB, firmware 15 MiB (ustar), log up to the last 4 KiB
 * The last 4 KiB are XNU's: HFS+ writes its alternate volume header 1 KiB before the partition's end as well.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/ioctl.h>
#include <linux/fs.h>
#endif

#include <zlib.h>

#include "mapple-install.h"

#define CHAIN_OFFSET	0x40000
#define MAX_RANGES	4096

struct region { const char *name; uint64_t from_end, size; };

static const struct region tail[] = {
	{ "settings", 64 * MiB, 1 * MiB },
	{ "firmware", 63 * MiB, 15 * MiB },
	{ "log", 48 * MiB, 48 * MiB - 4096 },
};

static const struct region slots[] = {	/* size 0: a record, no slot */
	{ "boot", 127 * MiB, 48 * MiB },
	{ "chained", 0, 0 },
};

void die(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	fputs("mapple-install: ", stderr);
	vfprintf(stderr, fmt, ap);
	fputc('\n', stderr);
	va_end(ap);
	exit(1);
}

void pread_all(int fd, void *buf, size_t n, uint64_t off)
{
	while (n) {
		ssize_t r = pread(fd, buf, n, (off_t)off);
		if (r <= 0)
			die("read at %llu: %s", (unsigned long long)off, r ? strerror(errno) : "end of file");
		buf = (char *)buf + r; n -= (size_t)r; off += (uint64_t)r;
	}
}

void pwrite_all(int fd, const void *buf, size_t n, uint64_t off)
{
	while (n) {
		ssize_t r = pwrite(fd, buf, n, (off_t)off);
		if (r <= 0)
			die("write at %llu: %s", (unsigned long long)off, r ? strerror(errno) : "nothing written");
		buf = (const char *)buf + r; n -= (size_t)r; off += (uint64_t)r;
	}
}

uint64_t size_of(int fd)
{
	off_t n = lseek(fd, 0, SEEK_END);

	if (n < 0)
		die("size: %s", strerror(errno));
	return (uint64_t)n;
}

static void hex(const uint8_t *d, int n, char *out)
{
	for (int i = 0; i < n; i++)
		sprintf(out + 2 * i, "%02x", d[i]);
}

void sha256(const void *d, size_t n, uint8_t out[32])
{
	struct sha256 h;

	sha256_init(&h); sha256_update(&h, d, n); sha256_final(&h, out);
}

static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint64_t le64(const uint8_t *p) { return le32(p) | (uint64_t)le32(p + 4) << 32; }
static void put32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (uint8_t)(v >> 8 * i); }
static void put64(uint8_t *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }

int open_dev(const char *path, int flags)
{
	int fd = open(path, flags, 0644);

	if (fd < 0)
		die("%s: %s", path, strerror(errno));
	return fd;
}

int write_back_try(int fd, const char *what, const uint8_t *buf, size_t n, uint64_t off)
{
	static int count;
	const char *fail = getenv("MAPPLE_FAIL_AT");
	uint8_t want[32], got[32], *back = malloc(n ? n : 1);
	char w[65], g[65];
	int ok;

	if (!back) {
		fprintf(stderr, "mapple-install: %s: out of memory\n", what);
		return -1;
	}
	for (size_t done = 0; done < n;) {
		ssize_t r = pwrite(fd, buf + done, n - done, (off_t)(off + done));
		if (r <= 0) {
			fprintf(stderr, "mapple-install: %s: write: %s\n", what, r ? strerror(errno) : "nothing written");
			free(back);
			return -1;
		}
		done += (size_t)r;
	}
	if (fsync(fd)) {
		fprintf(stderr, "mapple-install: %s: fsync: %s\n", what, strerror(errno));
		free(back);
		return -1;
	}
#ifdef __linux__
	if (ioctl(fd, BLKFLSBUF, 0) && errno != ENOTTY) {
		fprintf(stderr, "mapple-install: %s: BLKFLSBUF: %s\n", what, strerror(errno));
		free(back);
		return -1;
	}
#endif
	for (size_t done = 0; done < n;) {
		ssize_t r = pread(fd, back + done, n - done, (off_t)(off + done));
		if (r <= 0) {
			fprintf(stderr, "mapple-install: %s: read back: %s\n", what, r ? strerror(errno) : "end of file");
			free(back);
			return -1;
		}
		done += (size_t)r;
	}
	if (fail && ++count == atoi(fail) && n)
		back[0] ^= 1;
	sha256(buf, n, want);
	sha256(back, n, got);
	free(back);
	hex(want, 32, w); hex(got, 32, g);
	ok = !memcmp(want, got, 32);
	if (ok)
		printf("%s: %zu bytes written and read back, sha256 %s\n", what, n, g);
	else
		fprintf(stderr, "mapple-install: %s read back: %s, written %s\n", what, g, w);
	return ok ? 0 : -1;
}

void write_back(int fd, const char *what, const uint8_t *buf, size_t n, uint64_t off)
{
	if (write_back_try(fd, what, buf, n, off))
		exit(1);
}

uint8_t *load(const char *path, size_t *n)
{
	int fd = open_dev(path, O_RDONLY);
	uint8_t *buf;

	*n = (size_t)size_of(fd);
	if (!(buf = malloc(*n ? *n : 1)))
		die("out of memory");
	pread_all(fd, buf, *n, 0);
	close(fd);
	return buf;
}

/* --- verify-root ----------------------------------------------------------------------------------------------- */

struct ranges {
	uint32_t block_size;
	uint64_t blocks;
	char sha256[65];
	int n;
	uint64_t start[MAX_RANGES], end[MAX_RANGES];
};

static void read_ranges(const char *path, struct ranges *r)
{
	FILE *f = fopen(path, "r");
	char line[256], hash[80];
	unsigned long long a, b;

	if (!f)
		die("%s: %s", path, strerror(errno));
	memset(r, 0, sizeof(*r));
	if (!fgets(line, sizeof(line), f) || strcmp(line, "mapple-root 1\n"))
		die("%s: not a root range list", path);
	while (fgets(line, sizeof(line), f)) {
		if (sscanf(line, "block_size %llu", &a) == 1)
			r->block_size = (uint32_t)a;
		else if (sscanf(line, "blocks %llu", &a) == 1)
			r->blocks = a;
		else if (sscanf(line, "sha256 %64s", hash) == 1)
			strcpy(r->sha256, hash);
		else if (sscanf(line, "range %llu %llu", &a, &b) == 2) {
			if (r->n == MAX_RANGES || b <= a || a < (r->n ? r->end[r->n - 1] : 0) || b > r->blocks)
				die("%s: bad range %llu %llu", path, a, b);
			r->start[r->n] = a; r->end[r->n] = b; r->n++;
		} else
			die("%s: %s", path, line);
	}
	fclose(f);
	if (!r->block_size || !r->blocks || strlen(r->sha256) != 64 || !r->n)
		die("%s: incomplete", path);
}

/* The rule mtkloader applies before it starts XNU: an HFS+ volume header at 1 KiB and the alternate one 1 KiB before
 * the volume's end, with the same block size, block count and creation date (a new one for every image built). */
static void volume_headers(int fd, const char *dev, const struct ranges *r)
{
	uint8_t h[512], alt[512];
	uint64_t end = r->blocks * r->block_size;

	pread_all(fd, h, sizeof(h), 1024);
	pread_all(fd, alt, sizeof(alt), end - 1024);
	if ((memcmp(h, "H+", 2) && memcmp(h, "HX", 2)) || be32(h + 40) != r->block_size || be32(h + 44) != r->blocks)
		die("%s: no HFS+ volume of this root at its start: fastboot flash it first", dev);
	if (memcmp(alt, h, 2) || memcmp(alt + 40, h + 40, 8) || memcmp(alt + 16, h + 16, 4))
		die("%s: the volume's alternate header is not this volume's: the flash did not finish", dev);
}

int verify_root(const char *ranges_path, const char *dev)
{
	static struct ranges r;
	static uint8_t buf[4 * MiB];
	struct sha256 h;
	uint8_t digest[32];
	char got[65];
	uint64_t total = 0;
	int fd = open_dev(dev, O_RDONLY);

	read_ranges(ranges_path, &r);
	if (size_of(fd) < r.blocks * r.block_size + AREAS)
		die("%s: %llu bytes, the root and its areas need %llu", dev, (unsigned long long)size_of(fd),
		    (unsigned long long)(r.blocks * r.block_size + AREAS));
	volume_headers(fd, dev, &r);
	sha256_init(&h);
	for (int k = 0; k < r.n; k++)
		for (uint64_t p = r.start[k] * r.block_size, e = r.end[k] * r.block_size; p < e;) {
			size_t chunk = e - p < sizeof(buf) ? (size_t)(e - p) : sizeof(buf);
			pread_all(fd, buf, chunk, p);
			sha256_update(&h, buf, chunk);
			p += chunk; total += chunk;
		}
	sha256_final(&h, digest);
	hex(digest, 32, got);
	if (strcmp(got, r.sha256))
		die("%s: the root's blocks hash to %s, not %s: flash it again", dev, got, r.sha256);
	close(fd);
	printf("verify-root: %s holds the root, %llu bytes in %d ranges, sha256 %s\n", dev, (unsigned long long)total, r.n, got);
	return 0;
}

/* --- put, save ------------------------------------------------------------------------------------------------- */

static const struct region *find(const struct region *t, size_t n, const char *name)
{
	for (size_t i = 0; i < n; i++)
		if (!strcmp(t[i].name, name))
			return &t[i];
	return NULL;
}

void tail_put(int fd, uint64_t end, const char *region, const uint8_t *d, size_t n)
{
	const struct region *reg = find(tail, sizeof(tail) / sizeof(tail[0]), region);
	uint8_t *buf;
	char what[64];

	if (!reg)
		die("%s: not a region of the tail (settings, firmware, log)", region);
	if (n > reg->size)
		die("%zu bytes, the %s region holds %llu", n, region, (unsigned long long)reg->size);
	if (!(buf = calloc(1, (size_t)reg->size)))
		die("out of memory");
	memcpy(buf, d, n);
	snprintf(what, sizeof(what), "put %s", region);
	write_back(fd, what, buf, (size_t)reg->size, end - reg->from_end);
	free(buf);
}

/*
 * The saved area's header (little endian): "mapple-saved" (16 bytes), u32 version 1, u32 items; at 64 + 64 * i an item:
 * name (16 bytes), u64 size, u64 0, SHA-256; at 4064 the SHA-256 of the 4064 bytes before it. An item lives in its
 * slot (slots[] above). The item is written first, then the header, so a header never names data not yet there.
 */
#define SAVED_MAX	62

void saved_put(int fd, uint64_t end, const char *name, const uint8_t *d, size_t n)
{
	const struct region *slot = find(slots, sizeof(slots) / sizeof(slots[0]), name);
	uint64_t base = end - AREAS;
	uint8_t head[4096], digest[32], *it;
	uint32_t count, i;
	char what[64];

	if (!slot)
		die("%s: not an item of the saved area", name);
	if (n > slot->size && slot->size)
		die("%zu bytes, the %s slot holds %llu", n, name, (unsigned long long)slot->size);
	pread_all(fd, head, sizeof(head), base);
	sha256(head, 4064, digest);
	count = le32(head + 20);
	if (memcmp(head, "mapple-saved\0\0\0\0", 16) || le32(head + 16) != 1 || count > SAVED_MAX ||
	    memcmp(head + 4064, digest, 32)) {
		memset(head, 0, sizeof(head));
		memcpy(head, "mapple-saved", 12);
		put32(head + 16, 1);
		count = 0;
	}
	for (i = 0; i < count && strncmp((char *)head + 64 + 64 * i, name, 16); i++)
		;
	if (i == count)
		put32(head + 20, ++count);
	it = head + 64 + 64 * i;
	memset(it, 0, 64);
	strncpy((char *)it, name, 16);
	put64(it + 16, n);
	sha256(d, n, it + 32);

	if (slot->size) {
		snprintf(what, sizeof(what), "save %s", name);
		write_back(fd, what, d, n, end - slot->from_end);
	}
	sha256(head, 4064, head + 4064);
	write_back(fd, "save header", head, sizeof(head), base);
}

/* put and save on a partition (or a file standing for one): its end is its size */
static int put(const char *dev, const char *where, const char *file)
{
	int fd = open_dev(dev, O_RDWR);
	size_t n;
	uint8_t *d = load(file, &n);
	uint64_t off;
	char *e;

	if (find(tail, sizeof(tail) / sizeof(tail[0]), where)) {
		tail_put(fd, size_of(fd), where, d, n);
	} else {
		off = strtoull(where, &e, 0);
		if (*e || e == where)
			die("%s: not a region (settings, firmware, log) or an offset", where);
		if (off + n > size_of(fd))
			die("%s: %zu bytes at %llu run past its end", dev, n, (unsigned long long)off);
		write_back(fd, dev, d, n, off);
	}
	close(fd);
	return 0;
}

static int save(const char *dev, const char *name, const char *file)
{
	int fd = open_dev(dev, O_RDWR);
	size_t n;
	uint8_t *d = load(file, &n);

	saved_put(fd, size_of(fd), name, d, n);
	close(fd);
	return 0;
}

/* --- chain ----------------------------------------------------------------------------------------------------- */

static uint8_t *gunzip(const uint8_t *in, size_t n, size_t *out_n)
{
	size_t cap = 64u << 20;
	uint8_t *out = malloc(cap);
	z_stream z = { 0 };

	if (!out || inflateInit2(&z, 16 + MAX_WBITS) != Z_OK)
		die("inflate");
	z.next_in = (Bytef *)in; z.avail_in = (uInt)n;
	z.next_out = out; z.avail_out = (uInt)cap;
	if (inflate(&z, Z_FINISH) != Z_STREAM_END)
		die("the Android kernel does not gunzip (or is over 64 MiB)");
	*out_n = z.total_out;
	inflateEnd(&z);
	return out;
}

/* gzip as chainboot.py makes it (level 9, mtime 0), but the header's OS byte fixed at 3: zlib writes 19 on macOS */
static uint8_t *gzip9(const uint8_t *in, size_t n, size_t *out_n)
{
	size_t cap = n + n / 100 + 1024;
	uint8_t *out = malloc(cap);
	z_stream z = { 0 };
	static const uint8_t head[10] = { 0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 2, 3 };
	uLong crc = crc32(0, in, (uInt)n);

	if (!out || deflateInit2(&z, 9, Z_DEFLATED, -MAX_WBITS, 8, Z_DEFAULT_STRATEGY) != Z_OK)
		die("deflate");
	memcpy(out, head, sizeof(head));
	z.next_in = (Bytef *)in; z.avail_in = (uInt)n;
	z.next_out = out + 10; z.avail_out = (uInt)(cap - 18);
	if (deflate(&z, Z_FINISH) != Z_STREAM_END)
		die("deflate did not finish");
	*out_n = 10 + z.total_out;
	deflateEnd(&z);
	put32(out + *out_n, (uint32_t)crc);
	put32(out + *out_n + 4, (uint32_t)n);
	*out_n += 8;
	return out;
}

static size_t pages(size_t n, size_t page) { return (n + page - 1) / page * page; }

int chain(int argc, char **argv)
{
	const char *boot = argv[0], *backup = NULL, *out_path = NULL;
	size_t n, img_n, prog_n, pay_n, gz_n, blob_n, off, page;
	uint8_t *d, *img, *prog, *pay, *gz, *blob, *android, *outbuf;
	uint32_t ksize, sizes[5];
	uint64_t linux_size, android_n, payload_off, total;
	const uint8_t *parts[5];
	struct sha1 id;
	int fd;

	for (int i = 3; i < argc; i += 2) {
		if (i + 1 == argc)
			die("%s needs a file", argv[i]);
		if (!strcmp(argv[i], "--backup")) backup = argv[i + 1];
		else if (!strcmp(argv[i], "--out")) out_path = argv[i + 1];
		else die("unknown option %s", argv[i]);
	}
	d = load(boot, &n);
	prog = load(argv[1], &prog_n);
	pay = load(argv[2], &pay_n);
	if (n < 2048 || memcmp(d, "ANDROID!", 8) || le32(d + 40) != 2)
		die("%s: not an Android boot image, header v2", boot);
	page = le32(d + 36);
	ksize = le32(d + 8);
	sizes[0] = ksize; sizes[1] = le32(d + 16); sizes[2] = le32(d + 24);
	sizes[3] = le32(d + 1632); sizes[4] = le32(d + 1648);
	off = page;
	for (int i = 0; i < 5; i++) {
		if (off + sizes[i] > n)
			die("%s: part %d runs past the end", boot, i);
		parts[i] = d + off;
		off += pages(sizes[i], page);
	}
	if (parts[0][0] == 0x1f && parts[0][1] == 0x8b) {
		img = gunzip(parts[0], ksize, &img_n);
	} else {
		img = (uint8_t *)parts[0];
		img_n = ksize;
	}
	if (img_n < 0x40 || memcmp(img + 0x38, "ARM\x64", 4))
		die("%s: the kernel is not an arm64 Image", boot);

	/* a loader is in front already: the Android Image is the one at CHAIN_OFFSET, res2 (0x28) its length */
	android = img; android_n = img_n;
	if (le64(img + 0x28)) {
		android_n = le64(img + 0x28);
		if (img_n < CHAIN_OFFSET + android_n || memcmp(img + CHAIN_OFFSET + 0x38, "ARM\x64", 4))
			die("%s: a chained kernel without the Android Image inside", boot);
		android = img + CHAIN_OFFSET;
		printf("chain: %s already carries a loader; chained again from its Android Image\n", boot);
	}
	linux_size = le64(android + 0x10);

	if (!pay_n)
		die("%s: empty payload", argv[2]);
	if (prog_n < 0x40 || memcmp(prog + 0x38, "ARM\x64", 4) || prog_n > CHAIN_OFFSET)
		die("%s: not an arm64 program of at most %u bytes", argv[1], CHAIN_OFFSET);
	payload_off = CHAIN_OFFSET + (android_n + 15) / 16 * 16;
	total = CHAIN_OFFSET + linux_size > payload_off + pay_n ? CHAIN_OFFSET + linux_size : payload_off + pay_n;
	blob_n = payload_off + pay_n;
	if (!(blob = calloc(1, blob_n)))
		die("out of memory");
	memcpy(blob, prog, prog_n);
	put64(blob + 0x10, (total + 0xfffff) & ~(uint64_t)0xfffff);
	put64(blob + 0x28, android_n);
	put64(blob + 0x30, payload_off);
	memcpy(blob + CHAIN_OFFSET, android, android_n);
	memcpy(blob + payload_off, pay, pay_n);
	gz = gzip9(blob, blob_n, &gz_n);

	off = page + pages(gz_n, page);
	for (int i = 1; i < 5; i++)
		off += pages(sizes[i], page);
	if (off > n)	/* BOOT is the whole partition (or a copy of it): its size is the room there is */
		die("the new boot image is %zu bytes, %s only %zu", off, boot, n);
	if (!(outbuf = calloc(1, off)))
		die("out of memory");
	memcpy(outbuf, d, page);
	put32(outbuf + 8, (uint32_t)gz_n);
	parts[0] = gz; sizes[0] = (uint32_t)gz_n;
	sha1_init(&id);
	off = page;
	for (int i = 0; i < 5; i++) {
		uint8_t len[4];
		put32(len, sizes[i]);
		sha1_update(&id, parts[i], sizes[i]);
		sha1_update(&id, len, 4);
		memcpy(outbuf + off, parts[i], sizes[i]);
		off += pages(sizes[i], page);
	}
	memset(outbuf + 576, 0, 32);
	sha1_final(&id, outbuf + 576);

	if (backup && android != img) {
		printf("chain: no backup: %s carries a loader already\n", boot);
	} else if (backup) {
		int b = open(backup, O_WRONLY | O_CREAT | O_TRUNC, 0644);
		if (b < 0)
			die("%s: %s", backup, strerror(errno));
		pwrite_all(b, d, n, 0);
		if (fsync(b) || close(b))
			die("%s: %s", backup, strerror(errno));
		printf("chain: %s kept as %s (%zu bytes)\n", boot, backup, n);
	}
	if (!out_path)
		out_path = boot;
	fd = open_dev(out_path, out_path == boot ? O_RDWR : O_RDWR | O_CREAT | O_TRUNC);
	printf("chain: loader %zu, Image %llu, payload %zu bytes\n", prog_n, (unsigned long long)android_n, pay_n);
	write_back(fd, out_path, outbuf, off, 0);
	close(fd);
	return 0;
}

/* --- ilifw ----------------------------------------------------------------------------------------------------- */

/*
 * The ILI7807S's touch firmware as the item mtkloader loads (mtkili.h has the layout), from the phone's
 * FW_NF_ILI7807S_*.bin: the same bytes as mapple/loader's mtk/tools/mkilifw.py, which describes the format.
 */
#define ILI_MAX		(160 * 1024)
#define ILI_HEAD	64

static uint32_t crc32_msb(const uint8_t *d, size_t n)
{
	uint32_t crc = 0xffffffff;

	while (n--) {
		crc ^= (uint32_t)*d++ << 24;
		for (int i = 0; i < 8; i++)
			crc = crc & 0x80000000 ? crc << 1 ^ 0x04c11db7 : crc << 1;
	}
	return crc;
}

static long hexn(const char *p, int n)
{
	long v = 0;

	while (n--) {
		int c = *p++;
		if (c >= '0' && c <= '9') v = v << 4 | (c - '0');
		else if (c >= 'a' && c <= 'f') v = v << 4 | (c - 'a' + 10);
		else if (c >= 'A' && c <= 'F') v = v << 4 | (c - 'A' + 10);
		else return -1;
	}
	return v;
}

static int ilifw(const char *in, const char *out_path)
{
	static uint8_t image[ILI_MAX];
	static const char *const names[4] = { "", "AP", "DATA", "TUNING" };
	long start[4] = { -1, -1, -1, -1 }, end[4] = { 0 }, fixed[4] = { -1, -1, -1, -1 }, mem[4];
	size_t n, len, off = ILI_HEAD;
	uint8_t *raw = load(in, &n), *item, *rest, *p = memchr(raw, ':', n), *e = raw + n;
	uint32_t ext = 0, h = 0x811c9dc5;
	int fd;

	if (!p)
		die("%s: no Intel HEX in it", in);
	memset(image, 0xff, sizeof(image));
	while (p < e) {
		uint8_t *q = p, *l;
		long cnt, addr, kind;

		while (q < e && *q != '\n' && *q != '\r')
			q++;
		l = p;
		p = q + 1;
		while (l < q && (*l == ' ' || *l == '\t'))
			l++;
		while (q > l && (q[-1] == ' ' || q[-1] == '\t'))
			q--;
		if (q - l < 11 || *l != ':')
			continue;
		cnt = hexn((char *)l + 1, 2); addr = hexn((char *)l + 3, 4); kind = hexn((char *)l + 7, 2);
		if (cnt < 0 || addr < 0 || kind < 0 || q - l < 9 + 2 * cnt)
			die("%s: a bad HEX record", in);
		const char *d = (char *)l + 9;
		if (cnt < (kind == 0xaf ? 7 : kind == 0xb0 ? 4 : kind == 0x02 || kind == 0x04 ? 2 : 0))
			die("%s: a short HEX record", in);
		if (kind == 0x04) {
			ext = (uint32_t)hexn(d, 4);
		} else if (kind == 0x02) {
			ext = (uint32_t)hexn(d, 4) >> 12;
		} else if (kind == 0xaf) {
			long b = hexn(d + 12, 2);
			if (b >= 1 && b <= 3) { start[b] = hexn(d, 6); end[b] = hexn(d + 6, 6); }
		} else if (kind == 0xb0) {
			long b = hexn(d + 6, 2);
			if (b >= 1 && b <= 3) fixed[b] = hexn(d, 6);
		} else if (kind == 0x00) {
			uint32_t at = (uint32_t)addr + (ext << 16);
			if (at + cnt > ILI_MAX)
				die("%s: data past %d KB at %#x", in, ILI_MAX / 1024, at);
			for (long i = 0; i < cnt; i++) {
				long v = hexn(d + 2 * i, 2);
				if (v < 0)
					die("%s: a bad HEX record", in);
				image[at + i] = (uint8_t)v;
			}
		}
	}
	for (int b = 1; b <= 3; b++)
		if (start[b] < 0 || end[b] < start[b] + 4 || end[b] >= ILI_MAX)
			die("%s: no block %s", in, names[b]);
	mem[1] = fixed[1] >= 0 ? fixed[1] : 0;
	mem[2] = fixed[2] >= 0 ? fixed[2] : 0x20610;
	mem[3] = fixed[3] >= 0 ? fixed[3] : mem[2] + end[2] - start[2] + 1;

	len = ILI_HEAD;
	for (int b = 1; b <= 3; b++)
		len += (size_t)(end[b] - start[b] + 1 + 15) / 16 * 16;
	if (!(item = calloc(1, len)))
		die("out of memory");
	rest = item + 16;
	for (int b = 1; b <= 3; b++) {
		const uint8_t *body = image + start[b];
		size_t bl = (size_t)(end[b] - start[b] + 1);
		uint32_t crc = crc32_msb(body, bl - 4);

		if (crc != be32(body + bl - 4))
			die("%s: block %s's CRC %08x is not the one it carries (%08x)", in, names[b], crc, be32(body + bl - 4));
		put32(rest + 16 * (b - 1), (uint32_t)mem[b]);
		put32(rest + 16 * (b - 1) + 4, (uint32_t)bl);
		put32(rest + 16 * (b - 1) + 8, crc);
		put32(rest + 16 * (b - 1) + 12, (uint32_t)off);
		memcpy(item + off, body, bl);
		off += (bl + 15) / 16 * 16;
		printf("ilifw: %-6s hex %06lx..%06lx -> chip %05lx, %6zu bytes, CRC %08x\n", names[b], start[b], end[b], mem[b], bl, crc);
	}
	for (size_t i = 16; i < len; i++)
		h = (h ^ item[i]) * 0x01000193;
	memcpy(item, "ILFW", 4);
	put32(item + 4, (uint32_t)len);
	put32(item + 8, h);
	put32(item + 12, 3);
	fd = open_dev(out_path, O_WRONLY | O_CREAT | O_TRUNC);
	pwrite_all(fd, item, len, 0);
	close(fd);
	return 0;
}

int main(int argc, char **argv)
{
	if (argc == 4 && !strcmp(argv[1], "verify-root"))
		return verify_root(argv[2], argv[3]);
	if (argc == 5 && !strcmp(argv[1], "put"))
		return put(argv[2], argv[3], argv[4]);
	if (argc == 5 && !strcmp(argv[1], "save"))
		return save(argv[2], argv[3], argv[4]);
	if (argc == 4 && !strcmp(argv[1], "ilifw"))
		return ilifw(argv[2], argv[3]);
	if (argc >= 5 && !strcmp(argv[1], "chain"))
		return chain(argc - 2, argv + 2);
#ifdef MAPPLE_SPLIT
	if (argc >= 2 && !strncmp(argv[1], "split-", 6))
		return split_main(argc - 1, argv + 1);
#endif
	fprintf(stderr, "usage: mapple-install verify-root RANGES DEV\n"
			"       mapple-install put DEV REGION|OFFSET FILE\n"
			"       mapple-install save DEV NAME FILE\n"
			"       mapple-install ilifw FIRMWARE OUT\n"
			"       mapple-install chain BOOT LOADER PAYLOAD [--backup FILE] [--out FILE]\n");
	return 2;
}
