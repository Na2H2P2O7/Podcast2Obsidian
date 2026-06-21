import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { homedir } from "node:os";

const DEFAULT_GROUP_ID = "-100XXXXXXXXXX";
const DEFAULT_WRAPPER = `${homedir()}/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh`;
const DEFAULT_PROFILE = "xiaoyuzhou";
const DEFAULT_BILIBILI_PROFILE = "bilibili";
const DEFAULT_YOUTUBE_PROFILE = "youtube";
const DEFAULT_LOG_DIR = `${homedir()}/.openclaw/workspace/projects/podcast2obsidian/logs`;
const XIAOYUZHOU_URL_RE = /https?:\/\/(?:www\.)?xiaoyuzhoufm\.com\/(?:episode|podcast)\/[^\s<>"\])]+/iu;
const BILIBILI_URL_RE = /https?:\/\/(?:(?:www|m)\.)?bilibili\.com\/video\/(BV[0-9A-Za-z]{10,})[^\s<>"\])]*|https?:\/\/b23\.tv\/[^\s<>"\])]+/iu;
const BILIBILI_BV_RE = /(?:^|[\s<([{])((?:BV)[0-9A-Za-z]{10,})(?=$|[\s>\])}，。,.!?！？;；:：])/u;
const YOUTUBE_URL_RE = /https?:\/\/(?:(?:www|m)\.)?(?:youtube\.com\/(?:watch\?[^\s<>"\])]*v=|shorts\/|embed\/|live\/)|youtu\.be\/)[0-9A-Za-z_-]{11}[^\s<>"\])]*/iu;
const APPLE_PODCASTS_URL_RE = /https?:\/\/podcasts\.apple\.com\/[^\s<>"\])?]*\?(?=[^\s<>"\])]*\bi=\d+)[^\s<>"\])]*/iu;
const POCKETCASTS_URL_RE = /https?:\/\/(?:pca\.st\/episode\/|(?:www\.)?pocketcasts\.com\/podcast\/)[^\s<>"\])]+/iu;
const GOODPODS_URL_RE = /https?:\/\/(?:www\.)?goodpods\.com\/(?:[a-z]{2}\/)?podcasts\/[^\s<>"\])]+\/[^\s<>"\])]+/iu;
const REDCIRCLE_AUDIO_URL_RE = /https?:\/\/audio\d*\.redcircle\.com\/episodes\/[0-9a-f-]+\/stream\.(?:mp3|m4a|aac|wav|ogg)[^\s<>"\])]*/iu;

function cfg(pluginConfig = {}) {
  return {
    telegramGroupId: String(pluginConfig.telegramGroupId || DEFAULT_GROUP_ID),
    wrapper: String(pluginConfig.wrapper || DEFAULT_WRAPPER),
    profile: String(pluginConfig.profile || DEFAULT_PROFILE),
    bilibiliProfile: String(pluginConfig.bilibiliProfile || DEFAULT_BILIBILI_PROFILE),
    youtubeProfile: String(pluginConfig.youtubeProfile || DEFAULT_YOUTUBE_PROFILE),
    logDir: String(pluginConfig.logDir || DEFAULT_LOG_DIR),
  };
}

function stripTrailingPunctuation(value) {
  return String(value || "").replace(/[\]\u3002,，.。;；:：!?！？]+$/u, "");
}

function extractTask(text, options) {
  const body = String(text || "");
  const xiaoyuzhouMatch = body.match(XIAOYUZHOU_URL_RE);
  if (xiaoyuzhouMatch?.[0]) {
    return {
      profile: options.profile,
      target: stripTrailingPunctuation(xiaoyuzhouMatch[0]),
      kind: "小宇宙链接",
    };
  }

  const pocketcastsUrl = body.match(POCKETCASTS_URL_RE);
  if (pocketcastsUrl?.[0]) {
    return {
      profile: options.profile,
      target: stripTrailingPunctuation(pocketcastsUrl[0]),
      kind: "Pocket Casts 链接",
    };
  }

  const goodpodsUrl = body.match(GOODPODS_URL_RE);
  if (goodpodsUrl?.[0]) {
    return {
      profile: options.profile,
      target: stripTrailingPunctuation(goodpodsUrl[0]),
      kind: "Goodpods 链接",
    };
  }

  const applePodcastsUrl = body.match(APPLE_PODCASTS_URL_RE);
  if (applePodcastsUrl?.[0]) {
    return {
      profile: options.profile,
      target: stripTrailingPunctuation(applePodcastsUrl[0]),
      kind: "Apple Podcasts 单集链接",
    };
  }

  const redcircleAudioUrl = body.match(REDCIRCLE_AUDIO_URL_RE);
  if (redcircleAudioUrl?.[0]) {
    return {
      profile: options.profile,
      target: stripTrailingPunctuation(redcircleAudioUrl[0]),
      kind: "RedCircle 音频直链",
    };
  }

  const biliUrl = body.match(BILIBILI_URL_RE);
  if (biliUrl?.[0]) {
    return {
      profile: options.bilibiliProfile,
      target: stripTrailingPunctuation(biliUrl[0]),
      kind: "Bilibili 链接",
    };
  }

  const youtubeUrl = body.match(YOUTUBE_URL_RE);
  if (youtubeUrl?.[0]) {
    return {
      profile: options.youtubeProfile,
      target: stripTrailingPunctuation(youtubeUrl[0]),
      kind: "YouTube 链接",
    };
  }

  const bv = body.match(BILIBILI_BV_RE);
  if (bv?.[1]) {
    return {
      profile: options.bilibiliProfile,
      target: stripTrailingPunctuation(bv[1]),
      kind: "Bilibili BV",
    };
  }

  return null;
}

function appendDispatcherLog(logPath, line) {
  try {
    mkdirSync(dirname(logPath), { recursive: true });
    appendFileSync(logPath, `${new Date().toISOString()} ${line}\n`, "utf8");
  } catch {
    // Do not fail inbound handling because diagnostic logging failed.
  }
}

export default definePluginEntry({
  id: "xiaoyuzhou-dispatcher",
  name: "Podcast2Obsidian Dispatcher",
  description: "Deterministically starts Podcast2Obsidian profile runs from Telegram group URLs.",
  register(api) {
    api.on("before_dispatch", async (event, ctx) => {
      const options = cfg(api.pluginConfig);
      const channel = String(event.channel || ctx?.channelId || "").toLowerCase();
      const conversationId = String(ctx?.conversationId || "");
      if (channel !== "telegram" || conversationId !== options.telegramGroupId) return;

      const text = [event.content, event.body].filter(Boolean).join("\n");
      const task = extractTask(text, options);
      if (!task) return;

      const runId = `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${process.pid}`;
      const dispatchLog = `${options.logDir}/xiaoyuzhou-dispatcher.log`;
      const outLog = `${options.logDir}/xiaoyuzhou-dispatcher-${task.profile}-${runId}.log`;
      const args = [options.wrapper, "--profile", task.profile, "--url", task.target, "--notify-chat-id", options.telegramGroupId];

      appendDispatcherLog(dispatchLog, `start run_id=${runId} profile=${task.profile} channel=${channel} conversation=${conversationId} target=${task.target} log=${outLog}`);
      const child = spawn("bash", args, {
        detached: true,
        stdio: ["ignore", "pipe", "pipe"],
        env: { ...process.env },
      });
      child.stdout.on("data", (chunk) => appendDispatcherLog(outLog, chunk.toString().trimEnd()));
      child.stderr.on("data", (chunk) => appendDispatcherLog(outLog, chunk.toString().trimEnd()));
      child.on("error", (error) => appendDispatcherLog(dispatchLog, `error run_id=${runId} ${error?.message || String(error)}`));
      child.on("exit", (code, signal) => appendDispatcherLog(dispatchLog, `exit run_id=${runId} code=${code ?? "null"} signal=${signal ?? "null"}`));
      child.unref();

      return {
        handled: true,
        text: `收到，已开始处理${task.kind}：${task.target}`,
      };
    }, { priority: 100, timeoutMs: 5000 });
  },
});
