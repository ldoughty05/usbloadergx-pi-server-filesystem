#pragma once
#include <gctypes.h>
class DeviceHandler { public:
  static DeviceHandler * Instance();
  bool MountAllUSB(bool spinUp = true);
  void UnMountAllUSB();
  static u16 GetUSBPartitionCount();
};
