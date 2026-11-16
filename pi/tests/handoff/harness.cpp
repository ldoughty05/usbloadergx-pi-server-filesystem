// Host-side harness: runs the loader's PiSync.cpp against the real wiipi service.
#include <arpa/inet.h>
#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <string>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64; typedef int32_t s32; typedef int64_t s64;

std::vector<std::string> calls;
static void log(const char *what) { calls.push_back(what); printf("  [loader] %s\n", what); }

extern "C" {
u64 gettime(void) { timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return (u64)(ts.tv_sec * 1000ULL + ts.tv_nsec / 1000000) * 60750ULL; }
u32 inet_addr(const char *cp) { in_addr a{}; inet_pton(AF_INET, cp, &a); return a.s_addr; }
s32 net_socket(u32, u32, u32) { return socket(AF_INET, SOCK_STREAM, 0); }
s32 net_fcntl(s32 s, u32, u32) { return fcntl(s, F_SETFL, fcntl(s, F_GETFL) | O_NONBLOCK); }
s32 net_connect(s32 s, void *addr, u32) {
	const u8 *raw = (const u8 *)addr; sockaddr_in sin{}; sin.sin_family = AF_INET;
	u16 port; memcpy(&port, raw + 2, 2); sin.sin_port = htons(port); memcpy(&sin.sin_addr.s_addr, raw + 4, 4);
	return connect(s, (sockaddr *)&sin, sizeof(sin)) < 0 ? -errno : 0; }
s32 net_write(s32 s, const void *d, s32 n) { ssize_t r = send(s, d, n, MSG_NOSIGNAL); return r < 0 ? -errno : (s32)r; }
s32 net_read(s32 s, void *d, s32 n) { ssize_t r = recv(s, d, n, 0); return r < 0 ? -errno : (s32)r; }
s32 net_close(s32 s) { return close(s); }
void gprintf(const char *f, ...) { va_list a; va_start(a, f); vprintf(f, a); va_end(a); }
const char *get_text(const char *m) { return m; }
u32 getipbynamecached(char *) { return 0; }
u32 getipbyname(char *) { return 0; }
void ShowProgress(s64, s64) {}
void ProgressStop() { log("ProgressStop"); }
s32 WBFS_Init(u32) { log("WBFS_Init"); return 0; }
s32 WBFS_OpenAll() { log("WBFS_OpenAll"); return 0; }
s32 WBFS_OpenPart(int) { log("WBFS_OpenPart"); return 0; }
void WBFS_CloseAll() { log("WBFS_CloseAll"); }
}
void ShowProgress(const char *, const char *m1, const char *, s64 done, s64 total, bool, bool) {
	static std::string lastMsg; if (lastMsg != m1) { printf("  [progress] %s (%lld/%lld)\n", m1, (long long)done, (long long)total); lastMsg = m1; } }
bool IsNetworkInit(void) { return true; }
extern "C" const char *fmt(const char *format, ...) { static char b[512]; va_list a; va_start(a, format); vsnprintf(b, sizeof(b), format, a); va_end(a); return b; }
std::string lastPrompt;
int WindowPrompt(const char *t, const char *m, const char *, const char *, const char *, const char *, int) { lastPrompt = m ? m : ""; printf("  [prompt] %s: %s\n", t, m ? m : ""); calls.push_back("prompt"); return 1; }
bool NetworkInitPrompt() { return true; }
bool isCacheCurrent() { return true; }

#include "Controls/DeviceHandler.hpp"
#include "GameCube/GCGames.h"
#include "SoundOperations/MusicPlayer.h"
#include "settings/CSettings.h"
#include "settings/GameTitles.h"
#include "usbloader/GameList.h"
#include "PiSync.h"
#include "menu.h"

static DeviceHandler dh; DeviceHandler *DeviceHandler::Instance() { return &dh; }
bool DeviceHandler::MountAllUSB(bool) { log("MountAllUSB"); return true; }
void DeviceHandler::UnMountAllUSB() { log("UnMountAllUSB"); }
u16 DeviceHandler::GetUSBPartitionCount() { return 1; }
static GCGames gc; GCGames *GCGames::Instance() { return &gc; } u32 GCGames::LoadAllGames(bool) { return 0; }
static MusicPlayer mp; MusicPlayer *MusicPlayer::Instance() { return &mp; }
bool MusicPlayer::Load(const char *, const bool) { return true; } void MusicPlayer::Stop() {} void MusicPlayer::SetLoop(u8) {} void MusicPlayer::SetVolume(int) {}
CSettings Settings; bool CSettings::Save() { log("Settings.Save"); return true; }
CGameTitles GameTitles; void CGameTitles::LoadTitlesFromGameTDB(const char *) {}
GameList gameList; int GameList::ReadGameList(bool) { log("ReadGameList"); return 0; } int GameList::FilterList(const wchar_t *) { return 0; }

int main(int argc, char **argv)
{
	memset((void *)&Settings, 0, sizeof(Settings));
	Settings.PiSync = 1; Settings.godmode = 1; Settings.PiSyncPort = atoi(argv[2]);
	snprintf(Settings.PiSyncHost, sizeof(Settings.PiSyncHost), "%s", argv[1]);
	std::string mode = argc > 3 ? argv[3] : "sync";

	if (mode == "unreachable") {
		u64 t = gettime(); bool pending = PiSync::CheckPending();
		printf("unreachable: pending=%d after %llums\n", pending, (unsigned long long)((gettime() - t) / 60750));
		return pending ? 1 : 0;
	}
	bool pending = PiSync::CheckPending();
	printf("CheckPending -> %d  \"%s\"\n", pending, pending ? PiSync::PromptText() : "");
	if (mode == "idle") return pending ? 1 : 0;
	if (!pending) return 1;
	int next = PiSync::Execute();
	printf("Execute -> %d (MENU_DISCLIST=%d)\n", next, MENU_DISCLIST);
	// the browser comes back and polls again right away: nothing may be pending now
	bool again = PiSync::CheckPending();
	printf("CheckPending after sync -> %d\n", again);
	std::string order; for (auto &c : calls) order += c + ",";
	printf("ORDER %s\n", order.c_str());
	return (next == MENU_DISCLIST && !again) ? 0 : 1;
}
