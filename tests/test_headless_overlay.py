"""Native outline geometry/owner lifecycle tests; no Win32, GUI or input calls."""
import ctypes
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from opencomputer.headless_overlay import (NativeOutline, OutlineError, _FAILED_NATIVE_RESOURCES,
                                           _Win32Bars, frame_layout, normalize_rect)


def until(condition, timeout=1.0):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if condition(): return
        time.sleep(0.005)
    raise AssertionError('Fake outline owner did not reach expected state')


class FakeBars:
    def __init__(self, *, excluded=True):
        self.windows = [11, 12, 13, 14]
        self.capture_excluded = [excluded]*4
        self.displays = [(0, 0, 2000, 1200)]
        self.scale = 96
        self.calls = []
        self.shown = False
        self.closed = False
        self.fail = False
        self.block_show = None

    def record(self, name, value=None):
        self.calls.append((name, threading.get_ident(), value))

    def screens(self): self.record('screens'); return self.displays
    def dpi(self, target): self.record('dpi', target); return self.scale
    def show(self, bars):
        self.record('show', bars)
        if self.block_show: self.block_show.wait(1)
        if self.fail: raise OSError('fake native placement failure')
        self.shown = True
    def hide(self): self.record('hide'); self.shown = False
    def pump(self): self.record('pump')
    def close(self): self.record('close'); self.closed = True


def target(x=100, y=100, width=640, height=400):
    return {'event':'target', 'selected_window':{'id':'123', 'pid':99,
        'rect':{'x':x, 'y':y, 'width':width, 'height':height}}}


class OutlineGeometryTests(unittest.TestCase):
    def test_outside_bars_never_intersect_capture_client(self):
        client = (100, 100, 640, 400)
        mode, bars = frame_layout(client, [(0,0,2000,1200)], 3)
        self.assertEqual(mode, 'outside')
        self.assertEqual(len(bars), 4)
        x,y,w,h = client
        for bx,by,bw,bh in bars:
            self.assertTrue(bx+bw <= x or bx >= x+w or by+bh <= y or by >= y+h)

    def test_negative_monitor_origins_and_adjacent_monitor_union(self):
        screens = [(-1920,0,1920,1080),(0,0,1920,1080)]
        self.assertEqual(frame_layout((-700,100,1000,500), screens,3)[0], 'outside')
        self.assertEqual(frame_layout((-1900,20,700,500), screens,3)[0], 'outside')

    def test_offscreen_edges_require_capture_excluded_inset(self):
        client = (0,0,1920,1080)
        screens = [(0,0,1920,1080)]
        with self.assertRaises(OutlineError): frame_layout(client,screens,3)
        mode,bars = frame_layout(client,screens,3,allow_inset=True)
        self.assertEqual(mode,'inset')
        self.assertEqual(bars[0],(0,0,1920,3))
        for x,y,w,h in bars:
            self.assertGreater(w,0); self.assertGreater(h,0)
            self.assertGreaterEqual(x,0); self.assertGreaterEqual(y,0)
            self.assertLessEqual(x+w,1920); self.assertLessEqual(y+h,1080)

    def test_virtual_desktop_holes_and_partly_offscreen_target_reject(self):
        screens = [(0,0,1000,1000),(1200,0,1000,1000)]
        with self.assertRaises(OutlineError): frame_layout((900,100,400,500),screens,3,allow_inset=True)
        with self.assertRaises(OutlineError): frame_layout((-100,0,400,500),[(0,0,1000,1000)],3,allow_inset=True)

    def test_invalid_rectangles_rejected_before_native_work(self):
        for rect in ({}, {'x':0,'y':0,'width':0,'height':1},
                     {'x':True,'y':0,'width':100,'height':100},
                     {'x':2**31,'y':0,'width':100,'height':100}):
            with self.subTest(rect=rect),self.assertRaises(ValueError): normalize_rect(rect)


class NativeBarBoundaryTests(unittest.TestCase):
    def bare(self):
        bars=_Win32Bars.__new__(_Win32Bars)
        bars.windows=[11,12]
        bars.atom,bars.brush,bars.instance,bars._previous_dpi=7,8,9,10
        bars._brush_owned_by_class=True
        bars._name='fake-owned-class'
        bars._expected_shown=False
        bars.cleanup_failures=()
        return bars

    def test_failed_native_cleanup_checks_all_results_and_retains_resources(self):
        bars=self.bare()
        existing={11,12}
        calls=[]
        def destroy(window):
            calls.append(('destroy',window))
            if window==11:return False
            existing.remove(window);return True
        bars.user=SimpleNamespace(IsWindow=lambda w:w in existing,DestroyWindow=destroy,
            UnregisterClassW=lambda *_:calls.append(('unregister',)) or False,
            SetThreadDpiAwarenessContext=lambda *_:calls.append(('dpi',)) or None)
        bars.gdi=SimpleNamespace(DeleteObject=lambda *_:calls.append(('delete',)) or False)
        self.addCleanup(lambda:_FAILED_NATIVE_RESOURCES.pop(id(bars),None))
        with self.assertRaisesRegex(OutlineError,'DestroyWindow.*UnregisterClassW.*SetThreadDpi'):
            bars.close()
        self.assertEqual(bars.windows,[11])
        self.assertEqual((bars.atom,bars.brush,bars._previous_dpi),(7,8,10))
        self.assertEqual(calls,[('destroy',11),('destroy',12),('unregister',),('dpi',)])
        self.assertIs(_FAILED_NATIVE_RESOURCES[id(bars)],bars)
        # A confirmed successful retry clears only the retained resources.
        bars.user.DestroyWindow=lambda w:existing.remove(w) or True
        bars.user.UnregisterClassW=lambda *_:True
        bars.user.SetThreadDpiAwarenessContext=lambda *_:123
        bars.gdi.DeleteObject=lambda *_:True
        bars.close()
        self.assertEqual(bars.windows,[])
        self.assertEqual((bars.atom,bars.brush,bars._previous_dpi),(None,None,None))
        self.assertNotIn(id(bars),_FAILED_NATIVE_RESOURCES)

    def test_cleanup_exception_still_attempts_other_windows_and_resources(self):
        bars=self.bare()
        bars.user=SimpleNamespace(IsWindow=Mock(return_value=True),DestroyWindow=Mock(side_effect=OSError('fake')),
            UnregisterClassW=Mock(return_value=False),SetThreadDpiAwarenessContext=Mock(return_value=None))
        bars.gdi=SimpleNamespace(DeleteObject=Mock(return_value=False))
        self.addCleanup(lambda:_FAILED_NATIVE_RESOURCES.pop(id(bars),None))
        with self.assertRaises(OutlineError):bars.close()
        self.assertEqual(bars.user.DestroyWindow.call_count,2)
        bars.user.UnregisterClassW.assert_called_once()
        bars.gdi.DeleteObject.assert_not_called()
        bars.user.SetThreadDpiAwarenessContext.assert_called_once()
        self.assertEqual(bars.windows,[11,12])

    def test_registered_class_deletes_brush_on_success_without_double_delete(self):
        bars=self.bare();bars.windows=[]
        bars.user=SimpleNamespace(UnregisterClassW=Mock(return_value=True),
            SetThreadDpiAwarenessContext=Mock(return_value=123))
        bars.gdi=SimpleNamespace(DeleteObject=Mock(return_value=False))
        bars.close()
        bars.gdi.DeleteObject.assert_not_called()
        self.assertEqual((bars.atom,bars.brush),(None,None))
        self.assertFalse(bars._brush_owned_by_class)
        self.assertEqual(bars.cleanup_failures,())

    def test_failed_unregister_retains_class_owned_brush(self):
        bars=self.bare();bars.windows=[]
        bars.user=SimpleNamespace(UnregisterClassW=Mock(return_value=False),
            SetThreadDpiAwarenessContext=Mock(return_value=123))
        bars.gdi=SimpleNamespace(DeleteObject=Mock(return_value=True))
        self.addCleanup(lambda:_FAILED_NATIVE_RESOURCES.pop(id(bars),None))
        with self.assertRaisesRegex(OutlineError,'UnregisterClassW'):bars.close()
        bars.gdi.DeleteObject.assert_not_called()
        self.assertEqual((bars.atom,bars.brush),(7,8))
        self.assertTrue(bars._brush_owned_by_class)

    def test_unregistered_brush_remains_ours_and_delete_result_is_checked(self):
        bars=self.bare();bars.windows=[];bars.atom=None;bars._brush_owned_by_class=False
        bars.user=SimpleNamespace(UnregisterClassW=Mock(),SetThreadDpiAwarenessContext=Mock(return_value=123))
        bars.gdi=SimpleNamespace(DeleteObject=Mock(return_value=False))
        self.addCleanup(lambda:_FAILED_NATIVE_RESOURCES.pop(id(bars),None))
        with self.assertRaisesRegex(OutlineError,'DeleteObject'):bars.close()
        self.assertEqual(bars.brush,8)
        bars.gdi.DeleteObject.assert_called_once_with(8)
        bars.user.UnregisterClassW.assert_not_called()
        bars.gdi.DeleteObject.return_value=True
        bars.close()
        self.assertIsNone(bars.brush)
        self.assertNotIn(id(bars),_FAILED_NATIVE_RESOURCES)

    def test_visibility_health_ignores_deliberate_hidden_but_rejects_hidden_expected_bar(self):
        bars=self.bare()
        class Message(ctypes.Structure):_fields_=[('message',ctypes.c_uint)]
        bars._message=Message
        visible={11:False,12:False}
        bars.user=SimpleNamespace(PeekMessageW=lambda *_:False,IsWindow=lambda *_:True,
            IsWindowVisible=lambda w:visible[w],ShowWindow=lambda w,_:visible.__setitem__(w,False))
        bars.pump()  # Newly created windows are intentionally hidden.
        bars._expected_shown=True;visible.update({11:True,12:True})
        bars.pump()
        visible[12]=False
        with self.assertRaisesRegex(OutlineError,'became hidden'):bars.pump()
        bars.hide()
        bars.pump()  # Terminal hiding is intentional, too.


class OutlineLifecycleTests(unittest.TestCase):
    def make(self, native=None):
        bars = native or FakeBars()
        outline = NativeOutline(native_factory=lambda:bars,poll_interval=0.005)
        self.addCleanup(outline.close)
        return outline,bars

    def test_ready_hidden_then_initial_show_ack_on_single_owner_thread(self):
        outline,bars = self.make()
        self.assertFalse(outline.diagnostics()['visible'])
        outline.emit(target())
        outline.wait_visible()
        self.assertTrue(outline.diagnostics()['visible'])
        self.assertTrue(bars.shown)
        self.assertEqual({thread for _,thread,_ in bars.calls},{outline._thread.ident})
        self.assertNotEqual(outline._thread.ident,threading.get_ident())

    def test_geometry_and_dpi_changes_are_followed(self):
        outline,bars = self.make()
        outline.emit(target()); outline.wait_visible()
        bars.scale = 192
        outline.update_target({'x':250,'y':150,'width':700,'height':450})
        until(lambda:outline.diagnostics()['rect']==(250,150,700,450))
        self.assertEqual(outline.diagnostics()['thickness'],6)
        self.assertEqual(outline.diagnostics()['bar_rects'][0],(244,144,712,6))

    def test_terminal_hide_cannot_be_undone_by_late_events_or_updates(self):
        outline,bars = self.make()
        outline.emit(target());outline.wait_visible()
        outline.hide()
        outline.emit({**target(),'event':'armed'})
        outline.update_target({'x':300,'y':200,'width':500,'height':400})
        until(lambda:not bars.shown)
        self.assertFalse(outline.diagnostics()['visible'])
        with self.assertRaises(OutlineError):outline.wait_visible(0.02)

    def test_stop_during_placement_never_acknowledges_visible(self):
        bars=FakeBars();bars.block_show=threading.Event()
        outline,_=self.make(bars)
        outline.emit(target())
        until(lambda:any(name=='show' for name,_,_ in bars.calls))
        outline.hide();bars.block_show.set()
        until(lambda:not bars.shown)
        self.assertFalse(outline.diagnostics()['visible'])

    def test_error_and_cancellation_events_terminally_hide(self):
        for kind in ('error','cancelled'):
            with self.subTest(event=kind):
                outline,bars=self.make()
                outline.emit(target());outline.wait_visible()
                outline.emit({'event':kind})
                until(lambda:not bars.shown)
                outline.emit(target())
                with self.assertRaises(OutlineError):outline.wait_visible(0.02)

    def test_close_failure_still_publishes_failed_hidden_and_reaps_thread(self):
        outline,bars=self.make()
        original=bars.close
        def fail_close():
            original()
            raise OSError('fake cleanup failure')
        bars.close=fail_close
        outline.close()
        self.assertTrue(outline.diagnostics()['failed'])
        self.assertFalse(outline.diagnostics()['visible'])
        self.assertFalse(outline.diagnostics()['thread_alive'])

    def test_changed_geometry_requires_new_placement_ack(self):
        outline,bars=self.make()
        outline.emit(target());outline.wait_visible()
        bars.block_show=threading.Event()
        outline.update_target({'x':300,'y':200,'width':500,'height':400})
        with self.assertRaises(OutlineError):outline.wait_visible(0.02)
        bars.block_show.set()
        outline.wait_visible()
        self.assertEqual(outline.diagnostics()['rect'],(300,200,500,400))

    def test_missing_or_minimized_target_hides_terminally(self):
        outline,bars=self.make()
        outline.emit(target());outline.wait_visible()
        outline.update_target(None)
        until(lambda:not bars.shown)
        outline.emit(target())
        self.assertFalse(outline.diagnostics()['visible'])

    def test_edge_inset_only_when_every_bar_reports_capture_exclusion(self):
        outline,bars=self.make()
        outline.emit(target(0,0,2000,1200));outline.wait_visible()
        self.assertEqual(outline.diagnostics()['edge_mode'],'inset')
        outline.close()
        bars=FakeBars(excluded=False)
        outline,_=self.make(bars)
        outline.emit(target(0,0,2000,1200))
        until(lambda:outline.diagnostics()['failed'])
        with self.assertRaises(OutlineError):outline.wait_visible()
        self.assertFalse(bars.shown)

    def test_native_placement_failure_is_reported_and_resources_closed(self):
        outline,bars=self.make()
        bars.fail=True;outline.emit(target())
        until(lambda:outline.diagnostics()['failed'])
        with self.assertRaises(OutlineError):outline.check_health()
        until(lambda:bars.closed)
        self.assertFalse(bars.shown)

    def test_startup_failure_does_not_leave_owner_thread_running(self):
        def fail():raise OSError('fake native startup failure')
        before={t.ident for t in threading.enumerate() if t.name=='headless-native-outline'}
        with self.assertRaises(OutlineError):NativeOutline(native_factory=fail)
        after={t.ident for t in threading.enumerate() if t.name=='headless-native-outline'}
        self.assertEqual(before,after)

    def test_close_is_idempotent_and_reaps_owned_thread(self):
        outline,bars=self.make()
        outline.emit(target());outline.wait_visible()
        outline.close();outline.close()
        self.assertFalse(outline.diagnostics()['thread_alive'])
        self.assertFalse(bars.shown);self.assertTrue(bars.closed)

    def test_import_does_not_load_tk_or_panel(self):
        probe="import sys;import opencomputer.headless_overlay;assert 'tkinter' not in sys.modules;assert 'opencomputer.app' not in sys.modules"
        result=subprocess.run([sys.executable,'-c',probe],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))


if __name__=='__main__':unittest.main()
