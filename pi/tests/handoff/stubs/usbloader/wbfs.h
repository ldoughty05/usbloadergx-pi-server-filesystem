#pragma once
#include <gctypes.h>
enum { WBFS_DEVICE_USB = 1, WBFS_DEVICE_SDHC };
extern "C" { s32 WBFS_Init(u32 device); s32 WBFS_OpenAll(); s32 WBFS_OpenPart(int part_num); void WBFS_CloseAll(); }
