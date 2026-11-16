#pragma once
#include <gctypes.h>
#define ticks_to_millisecs(ticks) (((u64)(ticks)/(u64)(60750)))
#define diff_ticks(tick0,tick1) (((u64)(tick1)<(u64)(tick0))?((u64)-1-(u64)(tick0)+(u64)(tick1)):((u64)(tick1)-(u64)(tick0)))
extern "C" u64 gettime(void);
