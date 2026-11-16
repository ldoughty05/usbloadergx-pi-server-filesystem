/****************************************************************************
 * Pi Sync
 * for USB Loader GX
 *
 * The Raspberry Pi presents an image file as a USB drive. Only one side may
 * use that drive at a time, so installing a game remotely works like this:
 *
 *   1. The loader asks the Pi over the LAN whether anything is waiting.
 *   2. The loader closes and unmounts the USB drive.
 *   3. The loader tells the Pi the drive is free. The Pi unplugs the virtual
 *      drive, copies the games in and plugs it back.
 *   4. The loader mounts the drive again and reloads its game lists.
 *
 * The protocol is plain HTTP with "key=value" lines; see pi/README.md.
 ***************************************************************************/
#include <errno.h>
#include <fcntl.h>
#include <network.h>
#include <ogc/lwp_watchdog.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <string>
#include <unistd.h>

#include "PiSync.h"
#include "Controls/DeviceHandler.hpp"
#include "GameCube/GCGames.h"
#include "SoundOperations/MusicPlayer.h"
#include "cache/cache.hpp"
#include "gecko.h"
#include "language/gettext.h"
#include "menu.h"
#include "network/dns.h"
#include "network/networkops.h"
#include "prompts/ProgressWindow.h"
#include "prompts/PromptWindows.h"
#include "settings/CSettings.h"
#include "settings/GameTitles.h"
#include "usbloader/GameList.h"
#include "usbloader/wbfs.h"
#include "utils/StringTools.h"

#define PISYNC_DEFAULT_PORT   8765
#define PISYNC_QUICK_TIMEOUT  1500   // ms, background polls must not stall the menu
#define PISYNC_LONG_TIMEOUT   5000   // ms, while the user is waiting anyway
#define PISYNC_POLL_INTERVAL  15000  // ms between background polls
#define PISYNC_RETRY_INTERVAL 120000 // ms before polling an unreachable Pi again
#define PISYNC_START_TIMEOUT  20000  // ms for the Pi to start after a release
#define PISYNC_LOST_TIMEOUT   120000 // ms without any answer before giving up

namespace
{
	struct PiStatus
	{
		bool attached;
		bool busy;
		bool accepted;
		int gen;
		int pending;
		int preparing;
		int newest;
		s64 done;
		s64 total;
		char message[96];
		char error[96];
	};

	bool polled = false;         // at least one poll attempt was made
	u64 lastPoll = 0;
	u32 pollInterval = PISYNC_POLL_INTERVAL;
	int knownGen = -1;           // drive generation that matches what we have mounted
	int declinedNewest = -1;     // newest queue entry the user said "Later" to
	PiStatus lastStatus;
	char promptText[200];

	u32 ElapsedMs(u64 since)
	{
		return ticks_to_millisecs(diff_ticks(since, gettime()));
	}

	bool IsDottedIP(const char *text)
	{
		int a, b, c, d;
		char extra;
		return sscanf(text, "%d.%d.%d.%d%c", &a, &b, &c, &d, &extra) == 4;
	}

	s32 OpenSocket(u32 ip, u16 port, u32 timeoutMs)
	{
		struct sockaddr_in sin;
		memset(&sin, 0, sizeof(sin));
		sin.sin_family = AF_INET;
		sin.sin_port = htons(port);
		sin.sin_addr.s_addr = ip;

		s32 sock = net_socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
		if (sock < 0)
			return sock;

		// Non-blocking, so an unreachable Pi cannot hang the loader
		net_fcntl(sock, F_SETFL, 4);

		u64 start = gettime();
		while (ElapsedMs(start) < timeoutMs)
		{
			s32 ret = net_connect(sock, (struct sockaddr *)&sin, sizeof(sin));
			if (ret >= 0 || ret == -EISCONN)
				return sock;
			if (ret != -EINPROGRESS && ret != -EALREADY)
			{
				net_close(sock);
				return ret;
			}
			usleep(10000);
		}
		net_close(sock);
		return -ETIMEDOUT;
	}

	//! Minimal HTTP/1.0 GET. Copies the response body into out.
	bool HttpGet(const char *path, char *out, int outSize, u32 timeoutMs)
	{
		if (Settings.PiSyncHost[0] == 0)
			return false;

		char host[sizeof(Settings.PiSyncHost)];
		strlcpy(host, Settings.PiSyncHost, sizeof(host));
		u16 port = (Settings.PiSyncPort > 0 && Settings.PiSyncPort < 65536) ? Settings.PiSyncPort : PISYNC_DEFAULT_PORT;

		u32 ip = IsDottedIP(host) ? inet_addr(host) : getipbynamecached(host);
		if (ip == 0 || ip == 0xFFFFFFFF)
			return false;

		s32 sock = OpenSocket(ip, port, timeoutMs);
		if (sock < 0)
			return false;

		char request[256];
		int len = snprintf(request, sizeof(request),
						   "GET %s HTTP/1.0\r\n"
						   "Host: %s\r\n"
						   "User-Agent: USBLoaderGX-PiSync\r\n"
						   "Connection: close\r\n\r\n",
						   path, host);
		if (len <= 0 || len >= (int)sizeof(request))
		{
			net_close(sock);
			return false;
		}

		u64 start = gettime();
		int sent = 0;
		while (sent < len && ElapsedMs(start) < timeoutMs)
		{
			s32 ret = net_write(sock, request + sent, len - sent);
			if (ret > 0)
				sent += ret;
			else
				usleep(10000);
		}
		if (sent < len)
		{
			net_close(sock);
			return false;
		}

		char response[1536];
		int received = 0;
		start = gettime();
		while (received < (int)sizeof(response) - 1 && ElapsedMs(start) < timeoutMs)
		{
			s32 ret = net_read(sock, response + received, sizeof(response) - 1 - received);
			if (ret > 0)
			{
				received += ret;
				start = gettime();
			}
			else if (ret == 0)
				break; // the Pi closed the connection: response complete
			else
				usleep(10000);
		}
		net_close(sock);
		response[received] = 0;

		// "HTTP/1.x 200 ..."
		if (received < 12 || strncmp(response, "HTTP/1.", 7) != 0 || strncmp(response + 9, "200", 3) != 0)
			return false;

		const char *body = strstr(response, "\r\n\r\n");
		if (!body)
			return false;

		strlcpy(out, body + 4, outSize);
		return true;
	}

	bool KeyIs(const char *line, int keyLen, const char *name)
	{
		return keyLen == (int)strlen(name) && strncmp(line, name, keyLen) == 0;
	}

	//! Parse the "key=value" lines the Pi answers with.
	bool ParseStatus(const char *body, PiStatus &st)
	{
		memset(&st, 0, sizeof(st));
		bool valid = false;

		const char *line = body;
		while (line && *line)
		{
			const char *end = strchr(line, '\n');
			int lineLen = end ? (int)(end - line) : (int)strlen(line);
			const char *eq = (const char *)memchr(line, '=', lineLen);
			if (eq)
			{
				int keyLen = (int)(eq - line);
				char value[96];
				int valLen = lineLen - keyLen - 1;
				if (valLen > (int)sizeof(value) - 1)
					valLen = sizeof(value) - 1;
				memcpy(value, eq + 1, valLen);
				value[valLen] = 0;
				if (valLen > 0 && value[valLen - 1] == '\r')
					value[valLen - 1] = 0;

				if (KeyIs(line, keyLen, "wiipi"))
					valid = true;
				else if (KeyIs(line, keyLen, "state"))
					st.attached = (strcmp(value, "attached") == 0);
				else if (KeyIs(line, keyLen, "busy"))
					st.busy = (atoi(value) != 0);
				else if (KeyIs(line, keyLen, "accepted"))
					st.accepted = (atoi(value) != 0);
				else if (KeyIs(line, keyLen, "gen"))
					st.gen = atoi(value);
				else if (KeyIs(line, keyLen, "pending"))
					st.pending = atoi(value);
				else if (KeyIs(line, keyLen, "preparing"))
					st.preparing = atoi(value);
				else if (KeyIs(line, keyLen, "newest"))
					st.newest = atoi(value);
				else if (KeyIs(line, keyLen, "done"))
					st.done = strtoll(value, NULL, 10);
				else if (KeyIs(line, keyLen, "total"))
					st.total = strtoll(value, NULL, 10);
				else if (KeyIs(line, keyLen, "message"))
					strlcpy(st.message, value, sizeof(st.message));
				else if (KeyIs(line, keyLen, "error"))
					strlcpy(st.error, value, sizeof(st.error));
			}
			line = end ? end + 1 : NULL;
		}

		return valid;
	}

	bool Request(const char *path, PiStatus &st, u32 timeoutMs)
	{
		char body[1024];
		if (!HttpGet(path, body, sizeof(body), timeoutMs))
			return false;
		return ParseStatus(body, st);
	}

	bool GetStatus(PiStatus &st, u32 timeoutMs)
	{
		return Request("/api/wii/status", st, timeoutMs);
	}

	//! Tell the Pi the drive is free. True if it is (going to be) working on it.
	bool Release(PiStatus &st)
	{
		// Retry: at this point the drive is already unmounted on our side
		for (int i = 0; i < 3; i++)
		{
			if (Request("/api/wii/release", st, PISYNC_LONG_TIMEOUT))
				return st.accepted || st.busy;
			usleep(500000);
		}
		return false;
	}

	//! Show progress until the Pi has plugged the drive back in.
	void WaitForPi(int startGen, PiStatus &st)
	{
		u64 started = gettime();
		u64 lastContact = gettime();
		bool sawBusy = st.busy;

		while (true)
		{
			usleep(500000);

			PiStatus now;
			if (!GetStatus(now, PISYNC_LONG_TIMEOUT))
			{
				if (ElapsedMs(lastContact) > PISYNC_LOST_TIMEOUT)
				{
					gprintf("PiSync: lost contact with the Pi\n");
					strlcpy(st.error, tr("Lost contact with the Raspberry Pi."), sizeof(st.error));
					return;
				}
				continue;
			}

			lastContact = gettime();
			st = now;
			if (now.busy)
				sawBusy = true;

			// Finished: a new drive generation and nothing running
			if (!now.busy && now.gen != startGen)
				return;

			// It never started (e.g. the Pi restarted): do not wait forever
			if (!now.busy && !sawBusy && ElapsedMs(started) > PISYNC_START_TIMEOUT)
				return;

			ShowProgress(tr("Pi Sync"),
						 now.message[0] ? now.message : tr("Waiting for the Raspberry Pi..."),
						 tr("Do not turn off the console."),
						 now.done, now.total > 0 ? now.total : 1, true, false);
		}
	}

	//! Mount the USB drive again and open the game partition(s).
	bool RemountUSB()
	{
		bool mounted = false;

		for (int i = 0; i < 4 && !mounted; i++)
		{
			// The drive needs a moment to enumerate after the Pi plugs it back in
			usleep(1500000);
			DeviceHandler::Instance()->UnMountAllUSB();
			mounted = DeviceHandler::Instance()->MountAllUSB(true);
		}

		if (!mounted)
			return false;

		if (Settings.partition >= DeviceHandler::GetUSBPartitionCount())
			Settings.partition = 0;

		if (WBFS_Init(WBFS_DEVICE_USB) < 0)
			return false;

		if (Settings.MultiplePartitions)
			WBFS_OpenAll();
		else
			WBFS_OpenPart(Settings.partition);

		return true;
	}

	void ReloadGameLists()
	{
		gameList.ReadGameList(); // not from the cache: the drive content changed
		GCGames::Instance()->LoadAllGames();
		GameTitles.LoadTitlesFromGameTDB(Settings.titlestxt_path);
		gameList.FilterList();
		isCacheCurrent();
	}
}

bool PiSync::CheckPending()
{
	if (!Settings.PiSync || Settings.SDMode || Settings.PiSyncHost[0] == 0)
		return false;

	if (!Settings.godmode && (Settings.ParentalBlocks & BLOCK_GAME_INSTALL))
		return false;

	if (!IsNetworkInit())
		return false;

	if (polled && ElapsedMs(lastPoll) < pollInterval)
		return false;

	PiStatus st;
	bool reachable = GetStatus(st, PISYNC_QUICK_TIMEOUT);
	polled = true;
	lastPoll = gettime();

	if (!reachable)
	{
		// Back off, so a Pi that is switched off does not make the menu stutter
		pollInterval = PISYNC_RETRY_INTERVAL;
		return false;
	}

	pollInterval = PISYNC_POLL_INTERVAL;
	lastStatus = st;

	// First contact: what we mounted at startup is the current state
	if (knownGen < 0)
		knownGen = st.gen;

	if (st.busy || st.gen != knownGen)
	{
		// The drive was changed (or is being changed) without us, e.g. by a
		// forced install from the web UI. What we have mounted is stale.
		snprintf(promptText, sizeof(promptText), "%s", tr("The Raspberry Pi changed the USB drive. Reload it now?"));
		return true;
	}

	if (st.pending > 0 && st.newest != declinedNewest)
	{
		snprintf(promptText, sizeof(promptText), "%i %s", st.pending, tr("change(s) are waiting on the Raspberry Pi. Install now?"));
		return true;
	}

	return false;
}

const char *PiSync::PromptText()
{
	return promptText;
}

void PiSync::Decline()
{
	declinedNewest = lastStatus.newest;
	knownGen = lastStatus.gen;
}

bool PiSync::Run(bool interactive)
{
	if (Settings.SDMode)
	{
		WindowPrompt(tr("Pi Sync"), tr("Pi Sync is not available in SD Card Mode."), tr("OK"));
		return false;
	}

	if (Settings.PiSyncHost[0] == 0)
	{
		WindowPrompt(tr("Pi Sync"), tr("Set the Pi Sync address first."), tr("OK"));
		return false;
	}

	if (!IsNetworkInit() && !NetworkInitPrompt())
		return false;

	PiStatus st;
	if (!GetStatus(st, PISYNC_LONG_TIMEOUT))
	{
		WindowPrompt(tr("Pi Sync"), tr("Could not reach the Raspberry Pi. Check the address and port, and that the service is running."), tr("OK"));
		return false;
	}

	bool changed = st.busy || (knownGen >= 0 && st.gen != knownGen);
	if (!changed && st.pending == 0)
	{
		knownGen = st.gen;
		if (interactive)
		{
			if (st.preparing > 0)
				WindowPrompt(tr("Pi Sync"), tr("The Raspberry Pi is still preparing games. Try again in a moment."), tr("OK"));
			else
				WindowPrompt(tr("Pi Sync"), tr("Nothing is waiting on the Raspberry Pi."), tr("OK"));
		}
		return false;
	}

	gprintf("PiSync: releasing the USB drive (gen %i, pending %i, busy %i)\n", st.gen, st.pending, st.busy);
	ShowProgress(tr("Pi Sync"), tr("Releasing the USB drive..."), tr("Do not turn off the console."), 0, 1, true, false);

	// From here on nothing may touch the USB drive until the Pi hands it back
	bool musicOnUSB = (strncasecmp(Settings.ogg_path, "usb", 3) == 0);
	if (musicOnUSB)
		MusicPlayer::Instance()->Stop();

	Settings.Save();
	WBFS_CloseAll();
	DeviceHandler::Instance()->UnMountAllUSB();

	int startGen = st.gen;
	bool working = st.busy;
	if (!working && st.pending > 0)
		working = Release(st);

	if (working)
		WaitForPi(startGen, st);

	ShowProgress(tr("Pi Sync"), tr("Reconnecting the USB drive..."), tr("Reloading game list now, please wait..."), 0, 1, true, false);

	bool mounted = RemountUSB();
	if (mounted)
		ReloadGameLists();

	if (musicOnUSB && mounted)
	{
		MusicPlayer::Instance()->Load(Settings.ogg_path, true);
		MusicPlayer::Instance()->SetVolume(Settings.volume);
		MusicPlayer::Instance()->SetLoop(Settings.musicloopmode);
	}

	ProgressStop();

	// Whatever the Pi did, our view of the drive is fresh again
	PiStatus after;
	if (GetStatus(after, PISYNC_LONG_TIMEOUT))
	{
		knownGen = after.gen;
		lastStatus = after;
	}
	else
		knownGen = st.gen;
	declinedNewest = -1;
	polled = true;
	lastPoll = gettime();

	if (!mounted)
	{
		WindowPrompt(tr("Pi Sync"), tr("The USB drive did not come back. Check the Raspberry Pi, then restart USB Loader GX."), tr("OK"));
		return false;
	}

	if (st.error[0])
		WindowPrompt(tr("Pi Sync"), fmt("%s %s", tr("The Raspberry Pi reported a problem:"), st.error), tr("OK"));
	else if (interactive)
		WindowPrompt(tr("Pi Sync"), tr("The USB drive is up to date."), tr("OK"));

	return true;
}

int PiSync::Execute()
{
	Run(false);

	return MENU_DISCLIST;
}
