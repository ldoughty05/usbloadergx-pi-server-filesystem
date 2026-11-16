#pragma once
#include <gctypes.h>
enum { OFF, ON, MAX_ON_OFF };
enum { BLOCK_GAME_INSTALL = 0x4000 };
class CSettings { public: bool Save(); short PiSync; int PiSyncPort; char PiSyncHost[64]; short SDMode; short godmode; u32 ParentalBlocks; short partition; short MultiplePartitions; char ogg_path[150]; short volume; short musicloopmode; char titlestxt_path[100]; };
extern CSettings Settings;
