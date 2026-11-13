/* mapple-install's core, for verbs built in from elsewhere (splitboot's split-*, with -DMAPPLE_SPLIT). */
#ifndef MAPPLE_INSTALL_H
#define MAPPLE_INSTALL_H
#include <stddef.h>
#include <stdint.h>

#include "sha.h"

#define MiB	(1ull << 20)
#define AREAS	(128 * MiB)	/* the saved area and the tail, at the end of the root's partition */

void die(const char *fmt, ...) __attribute__((noreturn, format(printf, 1, 2)));
int open_dev(const char *path, int flags);
uint64_t size_of(int fd);
uint8_t *load(const char *path, size_t *n);
void pread_all(int fd, void *buf, size_t n, uint64_t off);
void pwrite_all(int fd, const void *buf, size_t n, uint64_t off);
void sha256(const void *d, size_t n, uint8_t out[32]);

/* write, fsync, drop the device's cache, read back, compare; MAPPLE_FAIL_AT=N makes the Nth read-back wrong */
int write_back_try(int fd, const char *what, const uint8_t *buf, size_t n, uint64_t off);
void write_back(int fd, const char *what, const uint8_t *buf, size_t n, uint64_t off);

/* END is the byte offset (on fd) where the root's partition ends */
void tail_put(int fd, uint64_t end, const char *region, const uint8_t *d, size_t n);
void saved_put(int fd, uint64_t end, const char *name, const uint8_t *d, size_t n);

int verify_root(const char *ranges_path, const char *dev);
int chain(int argc, char **argv);

#ifdef MAPPLE_SPLIT
int split_main(int argc, char **argv);
#endif
#endif
