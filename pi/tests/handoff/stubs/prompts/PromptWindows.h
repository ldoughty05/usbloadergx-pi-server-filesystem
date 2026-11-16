#pragma once
#include <gctypes.h>
int WindowPrompt(const char *title, const char *msg = NULL, const char * btn1Label = NULL, const char * btn2Label = NULL, const char * btn3Label = NULL, const char * btn4Label = NULL, int wait = -1);
bool NetworkInitPrompt();
