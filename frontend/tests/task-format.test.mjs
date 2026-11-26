import test from "node:test";
import assert from "node:assert/strict";
import { levelLabel, formatVoxelSize, pickDefaultLevel, timeLeft, taskPercent, taskTitle, lastLogMessage } from "../js/task-format.js";

test("level labels", () => {
  assert.equal(levelLabel(0), "Full resolution");
  assert.equal(levelLabel(1), "1/2 resolution");
  assert.equal(levelLabel(3), "1/8 resolution");
});

test("voxel size", () => {
  assert.equal(formatVoxelSize([10, 10, 10], "um"), "10 \u00b5m");
  assert.equal(formatVoxelSize([10, 10, 20], "um"), "10 \u00d7 10 \u00d7 20 \u00b5m");
  assert.equal(formatVoxelSize([0.0123456, 0.0123456, 0.0123456], "mm"), "0.01235 mm");
  assert.equal(formatVoxelSize([1, 1, 1], ""), "1");
  assert.equal(formatVoxelSize([2, 2, 2], "furlong"), "2 furlong");
  assert.equal(formatVoxelSize(null, "mm"), "");
});

test("the default level is the finest one of at most a gigabyte", () => {
  const G = 1024 ** 3;
  const levels = [{ level: 0, bytes: 40 * G }, { level: 1, bytes: 5 * G }, { level: 2, bytes: 0.6 * G }, { level: 3, bytes: 0.1 * G }];
  assert.equal(pickDefaultLevel(levels), 2);
  assert.equal(pickDefaultLevel([{ level: 0, bytes: 0.2 * G }, { level: 1, bytes: 0.02 * G }]), 0);
  assert.equal(pickDefaultLevel([{ level: 0, bytes: 9 * G }, { level: 1, bytes: 3 * G }]), 1); // none fits: the coarsest
  assert.equal(pickDefaultLevel([{ level: 0, bytes: 9 * G, allowed: false }, { level: 1, bytes: 0.5 * G }]), 1);
  assert.equal(pickDefaultLevel([{ level: 0, bytes: 9 * G, allowed: false }]), null);
});

test("time left", () => {
  const now = Date.parse("2026-11-25T12:00:00Z");
  const at = (ms) => new Date(now + ms).toISOString();
  assert.equal(timeLeft(at(23 * 3600e3 + 10 * 60e3), now), "23 h 10 min left");
  assert.equal(timeLeft(at(45 * 60e3), now), "45 min left");
  assert.equal(timeLeft(at(20e3), now), "less than a minute left");
  assert.equal(timeLeft(at(50 * 3600e3), now), "2 d 2 h left");
  assert.equal(timeLeft(at(-1000), now), "expired");
  assert.equal(timeLeft(null, now), "");
});

test("progress", () => {
  assert.equal(taskPercent({ progress_done: 35, progress_total: 70 }), 50);
  assert.equal(taskPercent({ progress_done: 70, progress_total: 70 }), 100);
  assert.equal(taskPercent({ progress_done: 0, progress_total: 0 }), null);
  assert.equal(taskPercent(null), null);
});

test("titles and failure reasons", () => {
  assert.equal(taskTitle({ kind: "tiff_export", params: { level: 2 } }), "TIFF stack \u00b7 1/4 resolution");
  assert.equal(taskTitle({ kind: "tiff_export", params: {} }), "TIFF stack \u00b7 full resolution");
  assert.equal(taskTitle({ kind: "something_new", params: {} }), "something_new");
  assert.equal(lastLogMessage({ status_log: ["12:00:01  Queued", "12:00:09  Failed: no space"] }), "Failed: no space");
  assert.equal(lastLogMessage({ status_log: [] }), "");
});
