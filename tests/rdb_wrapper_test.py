#!/usr/bin/env vpython3
# Copyright (c) 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for rdb_wrapper.py"""

from __future__ import print_function

import contextlib
import json
import logging
import os
import requests
import sys
import tempfile
import time
import unittest

if sys.version_info.major == 2:
  import mock
else:
  from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rdb_wrapper


@contextlib.contextmanager
def lucictx(ctx):
  try:
    orig = os.environ.get('LUCI_CONTEXT')

    if ctx is None:
      os.environ.pop('LUCI_CONTEXT', '')
      yield
    else:
      with tempfile.NamedTemporaryFile() as f:
        f.write(json.dumps(ctx).encode('utf-8'))
        f.flush()
        os.environ['LUCI_CONTEXT'] = f.name
        yield

  finally:
    if orig is None:
      os.environ.pop('LUCI_CONTEXT', '')
    else:
      os.environ['LUCI_CONTEXT'] = orig


@mock.patch.dict(os.environ, {})
class TestClient(unittest.TestCase):
  def test_without_lucictx(self):
    with lucictx(None):
      with rdb_wrapper.client("prefix") as s:
        self.assertIsNone(s)

    with lucictx({'something else': {'key': 'value'}}):
      with rdb_wrapper.client("prefix") as s:
        self.assertIsNone(s)

  def test_with_lucictx(self):
    with lucictx({'result_sink': {'address': '127', 'auth_token': 'secret'}}):
      with rdb_wrapper.client("prefix") as s:
        self.assertIsNotNone(s)
        self.assertEqual(
            s._url,
            'http://127/prpc/luci.resultsink.v1.Sink/ReportTestResults',
        )
        self.assertDictEqual(
            s._session.headers, {
                'Accept': 'application/json',
                'Authorization': 'ResultSink secret',
                'Content-Type': 'application/json',
            })


class TestResultSink(unittest.TestCase):
  def test_report(self):
    session = mock.MagicMock()
    sink = rdb_wrapper.ResultSink(session, 'http://host', 'test_id_prefix/')
    sink.report("function_foo", rdb_wrapper.STATUS_PASS, 123)
    expected = {
        'testId': 'test_id_prefix/function_foo',
        'status': rdb_wrapper.STATUS_PASS,
        'expected': True,
        'duration': '123.000000000s',
    }
    session.post.assert_called_once_with(
        'http://host',
        json={'testResults': [expected]},
    )


if __name__ == '__main__':
  logging.basicConfig(
      level=logging.DEBUG if '-v' in sys.argv else logging.ERROR)
  unittest.main()
