# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import sys

import config_util  # pylint: disable=import-error


# This class doesn't need an __init__ method, so we disable the warning
# pylint: disable=no-init
class CrashpadConfig(config_util.Config):
    """Basic Config class for Crashpad."""

    @staticmethod
    def fetch_spec(props):
        spec = {
            "solutions": [
                {
                    "name": "crashpad",
                    "url": "https://chromium.googlesource.com/crashpad/crashpad.git",
                    "managed": False,
                },
            ],
        }
        return {
            "type": "gclient_git",
            "gclient_git_spec": spec,
        }

    @staticmethod
    def expected_root(_props):
        return "crashpad"


def main(argv=None):
    return CrashpadConfig().handle_args(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
