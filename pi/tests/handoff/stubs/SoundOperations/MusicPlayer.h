#pragma once
#include <gctypes.h>
class MusicPlayer { public: static MusicPlayer * Instance(); bool Load(const char *path, const bool forceNewRandom = false); void Stop(); void SetLoop(u8 mode); void SetVolume(int volume); };
