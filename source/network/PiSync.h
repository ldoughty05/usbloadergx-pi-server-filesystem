/****************************************************************************
 * Pi Sync
 * for USB Loader GX
 *
 * Hands the USB drive over to a Raspberry Pi that emulates it (see pi/ in the
 * repository root), so games queued on the Pi can be installed without
 * unplugging anything, then mounts the drive again and reloads the game list.
 ***************************************************************************/
#ifndef _PISYNC_H_
#define _PISYNC_H_

namespace PiSync
{
	//! Poll the Pi. Cheap to call every frame: it only talks to the network
	//! every few seconds. Returns true when the user should be asked to sync.
	bool CheckPending();
	//! Message for the prompt after CheckPending() returned true.
	const char *PromptText();
	//! The user does not want to sync now: do not ask again for the same state.
	void Decline();
	//! Do the hand-off. Must not be called while anything (game browser, cover
	//! loaders, ...) is using the USB drive. Returns true if the drive was reloaded.
	bool Run(bool interactive);
	//! Run() as a menu state. Returns the menu to show next.
	int Execute();
}

#endif
