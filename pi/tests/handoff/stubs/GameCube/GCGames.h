#pragma once
#include <gctypes.h>
class GCGames { public: static GCGames *Instance(); u32 LoadAllGames(bool use_cache = false); };
