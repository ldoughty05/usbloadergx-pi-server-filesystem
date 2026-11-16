#pragma once
#include <gctypes.h>
#include <sys/types.h>
typedef u32 socklen_t;
#undef AF_INET
#define AF_INET 2
#define SOCK_STREAM 1
#define IPPROTO_IP 0
struct in_addr { u32 s_addr; };
struct sockaddr_in { u8 sin_len; u8 sin_family; u16 sin_port; struct in_addr sin_addr; s8 sin_zero[8]; };
struct sockaddr { u8 sa_len; u8 sa_family; s8 sa_data[14]; };
#define htons(x) (x)
#ifdef __cplusplus
extern "C" {
#endif
u32 inet_addr(const char *cp);
s32 net_socket(u32 domain,u32 type,u32 protocol);
s32 net_connect(s32 s,struct sockaddr *,socklen_t);
s32 net_write(s32 s,const void *data,s32 size);
s32 net_read(s32 s,void *mem,s32 len);
s32 net_close(s32 s);
s32 net_fcntl(s32 s, u32 cmd, u32 flags);
#ifdef __cplusplus
}
#endif
