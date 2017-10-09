#!/usr/bin/env python
# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A git command for managing a local cache of git repositories."""

from __future__ import print_function
import errno
import logging
import optparse
import os
import re
import tempfile
import threading
import time
import subprocess
import sys
import urlparse
import zipfile

from download_from_google_storage import Gsutil
import gclient_utils
import subcommand

# Analogous to gc.autopacklimit git config.
GC_AUTOPACKLIMIT = 50

GIT_CACHE_CORRUPT_MESSAGE = 'WARNING: The Git cache is corrupt.'

try:
  # pylint: disable=undefined-variable
  WinErr = WindowsError
except NameError:
  class WinErr(Exception):
    pass

class ClobberNeeded(Exception):
  pass


def exponential_backoff_retry(fn, excs=(Exception,), name=None, count=10,
                              sleep_time=0.25, printerr=None):
  """Executes |fn| up to |count| times, backing off exponentially.

  Args:
    fn (callable): The function to execute. If this raises a handled
        exception, the function will retry with exponential backoff.
    excs (tuple): A tuple of Exception types to handle. If one of these is
        raised by |fn|, a retry will be attempted. If |fn| raises an Exception
        that is not in this list, it will immediately pass through. If |excs|
        is empty, the Exception base class will be used.
    name (str): Optional operation name to print in the retry string.
    count (int): The number of times to try before allowing the exception to
        pass through.
    sleep_time (float): The initial number of seconds to sleep in between
        retries. This will be doubled each retry.
    printerr (callable): Function that will be called with the error string upon
        failures. If None, |logging.warning| will be used.

  Returns: The return value of the successful fn.
  """
  printerr = printerr or logging.warning
  for i in xrange(count):
    try:
      return fn()
    except excs as e:
      if (i+1) >= count:
        raise

      printerr('Retrying %s in %.2f second(s) (%d / %d attempts): %s' % (
          (name or 'operation'), sleep_time, (i+1), count, e))
      time.sleep(sleep_time)
      sleep_time *= 2


class Mirror(object):

  git_exe = 'git.bat' if sys.platform.startswith('win') else 'git'
  gsutil_exe = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'gsutil.py')

  @staticmethod
  def parse_fetch_spec(spec):
    """Parses and canonicalizes a fetch spec.

    Returns (fetchspec, value_regex), where value_regex can be used
    with 'git config --replace-all'.
    """
    parts = spec.split(':', 1)
    src = parts[0].lstrip('+').rstrip('/')
    if not src.startswith('refs/'):
      src = 'refs/heads/%s' % src
    dest = parts[1].rstrip('/') if len(parts) > 1 else src
    regex = r'\+%s:.*' % src.replace('*', r'\*')
    return ('+%s:%s' % (src, dest), regex)

  def __init__(self, url, refs=None, print_func=None):
    self.url = url
    self.fetch_specs = set([self.parse_fetch_spec(ref) for ref in (refs or [])])
    self.basedir = self.UrlToCacheDir(url)
    self.mirror_path = os.path.join(self.GetCachePath(), self.basedir)
    if print_func:
      self.print = self.print_without_file
      self.print_func = print_func
    else:
      self.print = print

  def print_without_file(self, message, **_kwargs):
    self.print_func(message)

  @property
  def bootstrap_bucket(self):
    u = urlparse.urlparse(self.url)
    if u.netloc == 'chromium.googlesource.com':
      return 'chromium-git-cache'
    elif u.netloc == 'chrome-internal.googlesource.com':
      return 'chrome-git-cache'
    # Not recognized.
    return None

  @classmethod
  def FromPath(cls, path):
    return cls(cls.CacheDirToUrl(path))

  @staticmethod
  def UrlToCacheDir(url):
    """Convert a git url to a normalized form for the cache dir path."""
    parsed = urlparse.urlparse(url)
    norm_url = parsed.netloc + parsed.path
    if norm_url.endswith('.git'):
      norm_url = norm_url[:-len('.git')]
    return norm_url.replace('-', '--').replace('/', '-').lower()

  @staticmethod
  def CacheDirToUrl(path):
    """Convert a cache dir path to its corresponding url."""
    netpath = re.sub(r'\b-\b', '/', os.path.basename(path)).replace('--', '-')
    return 'https://%s' % netpath

  @classmethod
  def SetCachePath(cls, cachepath):
    setattr(cls, 'cachepath', cachepath)

  @classmethod
  def GetCachePath(cls):
    if not hasattr(cls, 'cachepath'):
      try:
        cachepath = subprocess.check_output(
            [cls.git_exe, 'config', '--global', 'cache.cachepath']).strip()
      except subprocess.CalledProcessError:
        cachepath = None
      if not cachepath:
        raise RuntimeError(
            'No global cache.cachepath git configuration found.')
      setattr(cls, 'cachepath', cachepath)
    return getattr(cls, 'cachepath')

  def Rename(self, src, dst):
    # This is somehow racy on Windows.
    # Catching OSError because WindowsError isn't portable and
    # pylint complains.
    exponential_backoff_retry(
        lambda: os.rename(src, dst),
        excs=(OSError,),
        name='rename [%s] => [%s]' % (src, dst),
        printerr=self.print)

  def RunGit(self, cmd, **kwargs):
    """Run git in a subprocess."""
    cwd = kwargs.setdefault('cwd', self.mirror_path)
    kwargs.setdefault('print_stdout', False)
    kwargs.setdefault('filter_fn', self.print)
    env = kwargs.get('env') or kwargs.setdefault('env', os.environ.copy())
    env.setdefault('GIT_ASKPASS', 'true')
    env.setdefault('SSH_ASKPASS', 'true')
    self.print('running "git %s" in "%s"' % (' '.join(cmd), cwd))
    gclient_utils.CheckCallAndFilter([self.git_exe] + cmd, **kwargs)

  def config(self, cwd=None):
    if cwd is None:
      cwd = self.mirror_path

    # Don't run git-gc in a daemon.  Bad things can happen if it gets killed.
    try:
      self.RunGit(['config', 'gc.autodetach', '0'], cwd=cwd)
    except subprocess.CalledProcessError:
      # Hard error, need to clobber.
      raise ClobberNeeded()

    # Don't combine pack files into one big pack file.  It's really slow for
    # repositories, and there's no way to track progress and make sure it's
    # not stuck.
    if self.supported_project():
      self.RunGit(['config', 'gc.autopacklimit', '0'], cwd=cwd)

    # Allocate more RAM for cache-ing delta chains, for better performance
    # of "Resolving deltas".
    self.RunGit(['config', 'core.deltaBaseCacheLimit',
                 gclient_utils.DefaultDeltaBaseCacheLimit()], cwd=cwd)

    self.RunGit(['config', 'remote.origin.url', self.url], cwd=cwd)
    self.RunGit(['config', '--replace-all', 'remote.origin.fetch',
                 '+refs/heads/*:refs/heads/*', r'\+refs/heads/\*:.*'], cwd=cwd)
    for spec, value_regex in self.fetch_specs:
      self.RunGit(
          ['config', '--replace-all', 'remote.origin.fetch', spec, value_regex],
          cwd=cwd)

  def bootstrap_repo(self, directory):
    """Bootstrap the repo from Google Stroage if possible.

    More apt-ly named bootstrap_repo_from_cloud_if_possible_else_do_nothing().
    """

    if not self.bootstrap_bucket:
      return False
    python_fallback = False
    if (sys.platform.startswith('win') and
        not gclient_utils.FindExecutable('7z')):
      python_fallback = True
    elif sys.platform.startswith('darwin'):
      # The OSX version of unzip doesn't support zip64.
      python_fallback = True
    elif not gclient_utils.FindExecutable('unzip'):
      python_fallback = True

    gs_folder = 'gs://%s/%s' % (self.bootstrap_bucket, self.basedir)
    gsutil = Gsutil(self.gsutil_exe, boto_path=None)
    # Get the most recent version of the zipfile.
    _, ls_out, _ = gsutil.check_call('ls', gs_folder)
    ls_out_sorted = sorted(ls_out.splitlines())
    if not ls_out_sorted:
      # This repo is not on Google Storage.
      return False
    latest_checkout = ls_out_sorted[-1]

    # Download zip file to a temporary directory.
    try:
      tempdir = tempfile.mkdtemp(prefix='_cache_tmp', dir=self.GetCachePath())
      self.print('Downloading %s' % latest_checkout)
      code = gsutil.call('cp', latest_checkout, tempdir)
      if code:
        return False
      filename = os.path.join(tempdir, latest_checkout.split('/')[-1])

      # Unpack the file with 7z on Windows, unzip on linux, or fallback.
      if not python_fallback:
        if sys.platform.startswith('win'):
          cmd = ['7z', 'x', '-o%s' % directory, '-tzip', filename]
        else:
          cmd = ['unzip', filename, '-d', directory]
        retcode = subprocess.call(cmd)
      else:
        try:
          with zipfile.ZipFile(filename, 'r') as f:
            f.printdir()
            f.extractall(directory)
        except Exception as e:
          self.print('Encountered error: %s' % str(e), file=sys.stderr)
          retcode = 1
        else:
          retcode = 0
    finally:
      # Clean up the downloaded zipfile.
      #
      # This is somehow racy on Windows.
      # Catching OSError because WindowsError isn't portable and
      # pylint complains.
      exponential_backoff_retry(
          lambda: gclient_utils.rm_file_or_tree(tempdir),
          excs=(OSError,),
          name='rmtree [%s]' % (tempdir,),
          printerr=self.print)

    if retcode:
      self.print(
          'Extracting bootstrap zipfile %s failed.\n'
          'Resuming normal operations.' % filename)
      return False
    return True

  def exists(self):
    return os.path.isfile(os.path.join(self.mirror_path, 'config'))

  def supported_project(self):
    """Returns true if this repo is known to have a bootstrap zip file."""
    u = urlparse.urlparse(self.url)
    return u.netloc in [
        'chromium.googlesource.com',
        'chrome-internal.googlesource.com']

  def _preserve_fetchspec(self):
    """Read and preserve remote.origin.fetch from an existing mirror.

    This modifies self.fetch_specs.
    """
    if not self.exists():
      return
    try:
      config_fetchspecs = subprocess.check_output(
          [self.git_exe, 'config', '--get-all', 'remote.origin.fetch'],
          cwd=self.mirror_path)
      for fetchspec in config_fetchspecs.splitlines():
        self.fetch_specs.add(self.parse_fetch_spec(fetchspec))
    except subprocess.CalledProcessError:
      logging.warn('Tried and failed to preserve remote.origin.fetch from the '
                   'existing cache directory.  You may need to manually edit '
                   '%s and "git cache fetch" again.'
                   % os.path.join(self.mirror_path, 'config'))

  def _ensure_bootstrapped(self, depth, bootstrap, force=False):
    tempdir = None
    pack_dir = os.path.join(self.mirror_path, 'objects', 'pack')
    pack_files = []

    if os.path.isdir(pack_dir):
      pack_files = [f for f in os.listdir(pack_dir) if f.endswith('.pack')]

    should_bootstrap = (force or
                        not self.exists() or
                        len(pack_files) > GC_AUTOPACKLIMIT)
    if should_bootstrap:
      if self.exists():
        # Re-bootstrapping an existing mirror; preserve existing fetch spec.
        self._preserve_fetchspec()
      tempdir = tempfile.mkdtemp(
          prefix='_cache_tmp', suffix=self.basedir, dir=self.GetCachePath())
      bootstrapped = not depth and bootstrap and self.bootstrap_repo(tempdir)
      if bootstrapped:
        # Bootstrap succeeded; delete previous cache, if any.
        gclient_utils.rmtree(self.mirror_path)
      elif not self.exists() or not self.supported_project():
        # Bootstrap failed due to either
        # 1. No previous cache
        # 2. Project doesn't have a bootstrap zip file
        # Start with a bare git dir.
        self.RunGit(['init', '--bare'], cwd=tempdir)
      else:
        # Bootstrap failed, previous cache exists; warn and continue.
        logging.warn(
            'Git cache has a lot of pack files (%d).  Tried to re-bootstrap '
            'but failed.  Continuing with non-optimized repository.'
            % len(pack_files))
        gclient_utils.rmtree(tempdir)
        tempdir = None
    else:
      if depth and os.path.exists(os.path.join(self.mirror_path, 'shallow')):
        logging.warn(
            'Shallow fetch requested, but repo cache already exists.')
    return tempdir

  def _fetch(self, rundir, verbose, depth):
    self.config(rundir)
    v = []
    d = []
    if verbose:
      v = ['-v', '--progress']
    if depth:
      d = ['--depth', str(depth)]
    fetch_cmd = ['fetch'] + v + d + ['origin']
    fetch_specs = subprocess.check_output(
        [self.git_exe, 'config', '--get-all', 'remote.origin.fetch'],
        cwd=rundir).strip().splitlines()
    for spec in fetch_specs:
      try:
        self.print('Fetching %s' % spec)
        self.RunGit(fetch_cmd + [spec], cwd=rundir, retry=True)
      except subprocess.CalledProcessError:
        if spec == '+refs/heads/*:refs/heads/*':
          raise ClobberNeeded()  # Corrupted cache.
        logging.warn('Fetch of %s failed' % spec)

  def populate(self, depth=None, shallow=False, bootstrap=False, verbose=False):
    assert self.GetCachePath()
    if shallow and not depth:
      depth = 10000
    gclient_utils.safe_makedirs(self.GetCachePath())

    tempdir = None
    try:
      tempdir = self._ensure_bootstrapped(depth, bootstrap)
      rundir = tempdir or self.mirror_path
      self._fetch(rundir, verbose, depth)
    except ClobberNeeded:
      # This is a major failure, we need to clean and force a bootstrap.
      gclient_utils.rmtree(rundir)
      self.print(GIT_CACHE_CORRUPT_MESSAGE)
      tempdir = self._ensure_bootstrapped(depth, bootstrap, force=True)
      assert tempdir
      self._fetch(tempdir or self.mirror_path, verbose, depth)
    finally:
      if tempdir:
        if os.path.exists(self.mirror_path):
          gclient_utils.rmtree(self.mirror_path)
        self.Rename(tempdir, self.mirror_path)

  def update_bootstrap(self, prune=False):
    # The files are named <git number>.zip
    gen_number = subprocess.check_output(
        [self.git_exe, 'number', 'master'], cwd=self.mirror_path).strip()
    # Run Garbage Collect to compress packfile.
    self.RunGit(['gc', '--prune=all'])
    # Creating a temp file and then deleting it ensures we can use this name.
    _, tmp_zipfile = tempfile.mkstemp(suffix='.zip')
    os.remove(tmp_zipfile)
    subprocess.call(['zip', '-r', tmp_zipfile, '.'], cwd=self.mirror_path)
    gsutil = Gsutil(path=self.gsutil_exe, boto_path=None)
    gs_folder = 'gs://%s/%s' % (self.bootstrap_bucket, self.basedir)
    dest_name = '%s/%s.zip' % (gs_folder, gen_number)
    gsutil.call('cp', tmp_zipfile, dest_name)
    os.remove(tmp_zipfile)

    # Remove all other files in the same directory.
    if prune:
      _, ls_out, _ = gsutil.check_call('ls', gs_folder)
      for filename in ls_out.splitlines():
        if filename == dest_name:
          continue
        gsutil.call('rm', filename)

  @staticmethod
  def DeleteTmpPackFiles(path):
    pack_dir = os.path.join(path, 'objects', 'pack')
    if not os.path.isdir(pack_dir):
      return
    pack_files = [f for f in os.listdir(pack_dir) if
                  f.startswith('.tmp-') or f.startswith('tmp_pack_')]
    for f in pack_files:
      f = os.path.join(pack_dir, f)
      try:
        os.remove(f)
        logging.warn('Deleted stale temporary pack file %s' % f)
      except OSError:
        logging.warn('Unable to delete temporary pack file %s' % f)


@subcommand.usage('[url of repo to check for caching]')
def CMDexists(parser, args):
  """Check to see if there already is a cache of the given repo."""
  _, args = parser.parse_args(args)
  if not len(args) == 1:
    parser.error('git cache exists only takes exactly one repo url.')
  url = args[0]
  mirror = Mirror(url)
  if mirror.exists():
    print(mirror.mirror_path)
    return 0
  return 1


@subcommand.usage('[url of repo to create a bootstrap zip file]')
def CMDupdate_bootstrap(parser, args):
  """Create and uploads a bootstrap tarball."""
  # Lets just assert we can't do this on Windows.
  if sys.platform.startswith('win'):
    print('Sorry, update bootstrap will not work on Windows.', file=sys.stderr)
    return 1

  parser.add_option('--prune', action='store_true',
                    help='Prune all other cached zipballs of the same repo.')

  # First, we need to ensure the cache is populated.
  populate_args = args[:]
  populate_args.append('--no-bootstrap')
  CMDpopulate(parser, populate_args)

  # Get the repo directory.
  options, args = parser.parse_args(args)
  url = args[0]
  mirror = Mirror(url)
  mirror.update_bootstrap(options.prune)
  return 0


@subcommand.usage('[url of repo to add to or update in cache]')
def CMDpopulate(parser, args):
  """Ensure that the cache has all up-to-date objects for the given repo."""
  parser.add_option('--depth', type='int',
                    help='Only cache DEPTH commits of history')
  parser.add_option('--shallow', '-s', action='store_true',
                    help='Only cache 10000 commits of history')
  parser.add_option('--ref', action='append',
                    help='Specify additional refs to be fetched')
  parser.add_option('--no_bootstrap', '--no-bootstrap',
                    action='store_true',
                    help='Don\'t bootstrap from Google Storage')

  options, args = parser.parse_args(args)
  if not len(args) == 1:
    parser.error('git cache populate only takes exactly one repo url.')
  url = args[0]

  mirror = Mirror(url, refs=options.ref)
  kwargs = {
      'verbose': options.verbose,
      'shallow': options.shallow,
      'bootstrap': not options.no_bootstrap,
  }
  if options.depth:
    kwargs['depth'] = options.depth
  mirror.populate(**kwargs)


@subcommand.usage('Fetch new commits into cache and current checkout')
def CMDfetch(parser, args):
  """Update mirror, and fetch in cwd."""
  parser.add_option('--all', action='store_true', help='Fetch all remotes')
  parser.add_option('--no_bootstrap', '--no-bootstrap',
                    action='store_true',
                    help='Don\'t (re)bootstrap from Google Storage')
  options, args = parser.parse_args(args)

  # Figure out which remotes to fetch.  This mimics the behavior of regular
  # 'git fetch'.  Note that in the case of "stacked" or "pipelined" branches,
  # this will NOT try to traverse up the branching structure to find the
  # ultimate remote to update.
  remotes = []
  if options.all:
    assert not args, 'fatal: fetch --all does not take a repository argument'
    remotes = subprocess.check_output([Mirror.git_exe, 'remote']).splitlines()
  elif args:
    remotes = args
  else:
    current_branch = subprocess.check_output(
        [Mirror.git_exe, 'rev-parse', '--abbrev-ref', 'HEAD']).strip()
    if current_branch != 'HEAD':
      upstream = subprocess.check_output(
          [Mirror.git_exe, 'config', 'branch.%s.remote' % current_branch]
      ).strip()
      if upstream and upstream != '.':
        remotes = [upstream]
  if not remotes:
    remotes = ['origin']

  cachepath = Mirror.GetCachePath()
  git_dir = os.path.abspath(subprocess.check_output(
      [Mirror.git_exe, 'rev-parse', '--git-dir']))
  git_dir = os.path.abspath(git_dir)
  if git_dir.startswith(cachepath):
    mirror = Mirror.FromPath(git_dir)
    mirror.populate(
        bootstrap=not options.no_bootstrap,)
    return 0
  for remote in remotes:
    remote_url = subprocess.check_output(
        [Mirror.git_exe, 'config', 'remote.%s.url' % remote]).strip()
    if remote_url.startswith(cachepath):
      mirror = Mirror.FromPath(remote_url)
      mirror.print = lambda *args: None
      print('Updating git cache...')
      mirror.populate(
          bootstrap=not options.no_bootstrap)
    subprocess.check_call([Mirror.git_exe, 'fetch', remote])
  return 0


class OptionParser(optparse.OptionParser):
  """Wrapper class for OptionParser to handle global options."""

  def __init__(self, *args, **kwargs):
    optparse.OptionParser.__init__(self, *args, prog='git cache', **kwargs)
    self.add_option('-c', '--cache-dir',
                    help='Path to the directory containing the cache')
    self.add_option('-v', '--verbose', action='count', default=1,
                    help='Increase verbosity (can be passed multiple times)')
    self.add_option('-q', '--quiet', action='store_true',
                    help='Suppress all extraneous output')

  def parse_args(self, args=None, values=None):
    options, args = optparse.OptionParser.parse_args(self, args, values)
    if options.quiet:
      options.verbose = 0

    levels = [logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG]
    logging.basicConfig(level=levels[min(options.verbose, len(levels) - 1)])

    try:
      global_cache_dir = Mirror.GetCachePath()
    except RuntimeError:
      global_cache_dir = None
    if options.cache_dir:
      if global_cache_dir and (
          os.path.abspath(options.cache_dir) !=
          os.path.abspath(global_cache_dir)):
        logging.warn('Overriding globally-configured cache directory.')
      Mirror.SetCachePath(options.cache_dir)

    return options, args


def main(argv):
  dispatcher = subcommand.CommandDispatcher(__name__)
  return dispatcher.execute(OptionParser(), argv)


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv[1:]))
  except KeyboardInterrupt:
    sys.stderr.write('interrupted\n')
    sys.exit(1)
