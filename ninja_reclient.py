#!/usr/bin/env python3
# Copyright 2023 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""This script is a wrapper around the ninja.py script that also
handles the client lifecycle safely. It will automatically start
reproxy before running ninja and stop reproxy when ninja stops
for any reason eg. build completes, keyboard interupt etc."""

import os
import subprocess
import sys

import ninja
import gclient_paths


class NotFoundError(Exception):
  """A file could not be found."""

  def __init__(self, e):
    super().__init__(
        'Problem while looking for reclient in Chromium source tree:\n'
        '%s' % e)


def find_reclient_bin_dir():
  tools_path = gclient_paths.GetBuildtoolsPath()
  if not tools_path:
    raise NotFoundError(
        'Could not find checkout in any parent of the current path.\n')

  reclient_bin_dir = os.path.join(tools_path, 'reclient')
  if os.path.isdir(reclient_bin_dir):
    return reclient_bin_dir
  raise NotFoundError(
      'Could not find reclient binaries in //buildtools/reclient.\n'
      'Try running gclient sync.')


def find_reclient_cfg():
  tools_path = gclient_paths.GetBuildtoolsPath()
  if not tools_path:
    raise NotFoundError(
        'Could not find checkout in any parent of the current path.\n')

  reclient_cfg = os.path.join(tools_path, 'reclient_cfgs', 'reproxy.cfg')
  if os.path.isfile(reclient_cfg):
    return reclient_cfg
  raise NotFoundError(
      'Could not find reproxy.cfg at //buildtools/reclient_cfgs/reproxy.cfg.\n'
      'Developer builds are not currently supported, use ninja.py for now.')


def run(cmd_args):
  if os.environ.get('NINJA_SUMMARIZE_BUILD') == '1':
    print(' '.join(cmd_args))
  return subprocess.call(cmd_args)


def start_reproxy(reclient_cfg, reclient_bin_dir):
  return run([
      os.path.join(reclient_bin_dir, 'bootstrap'),
      '--re_proxy=' + os.path.join(reclient_bin_dir, 'reproxy'),
      '--cfg=' + reclient_cfg
  ])


def stop_reproxy(reclient_cfg, reclient_bin_dir):
  return run([
      os.path.join(reclient_bin_dir, 'bootstrap'), '--shutdown',
      '--cfg=' + reclient_cfg
  ])


def main(argv):
  # If use_remoteexec is set, but the reclient binaries or configs don't
  # exist, display an error message and stop.  Otherwise, the build will
  # attempt to run with rewrapper wrapping actions, but will fail with
  # possible non-obvious problems.
  # As of January 2023, dev builds with reclient are not supported, so
  # indicate that use_goma should be swapped for use_remoteexec.  This
  # message will be changed when dev builds are fully supported.
  try:
    reclient_bin_dir = find_reclient_bin_dir()
    reclient_cfg = find_reclient_cfg()
  except NotFoundError:
    print(("Build is configured to use reclient but necessary binaries "
           "or config files can't be found.  Developer builds with "
           "reclient are not yet supported.  Try regenerating your "
           "build with use_goma in place of use_remoteexec for now."),
          file=sys.stderr)
    return 1
  reproxy_ret_code = start_reproxy(reclient_cfg, reclient_bin_dir)
  if reproxy_ret_code != 0:
    return reproxy_ret_code
  try:
    return ninja.main(argv)
  except KeyboardInterrupt:
    return 1
  finally:
    stop_reproxy(reclient_cfg, reclient_bin_dir)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
