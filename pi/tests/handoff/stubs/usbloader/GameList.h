#pragma once
#include <wchar.h>
class GameList { public: int ReadGameList(bool use_cache = false); int FilterList(const wchar_t * gameFilter = NULL); };
extern GameList gameList;
