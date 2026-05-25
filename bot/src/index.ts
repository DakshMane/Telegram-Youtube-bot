import 'dotenv/config';
import fs from 'fs';
import TelegramBot from 'node-telegram-bot-api';
import { PubSubManager } from './PubSubManager.js';

const token = process.env.BOT_TOKEN!;
const bot = new TelegramBot(token, { polling: true });

const urlRegex = /https?:\/\/(www\.)?(youtube\.com|youtu\.be|instagram\.com)\S+/i;

const pendingDownloads = new Map<string, string>();
const pendingSearchResults = new Map<string, any[]>();
const progressMessages = new Map<string, number>();

async function handleUrl(chatId: number, url: string) {
  const loadingMsg = await bot.sendMessage(chatId, '⏳ Fetching video info...');

  try {
    const bus = PubSubManager.getInstance();
    const meta = await bus.request('meta:request', `meta:response:${chatId}`, { url, chatId });

    await bot.deleteMessage(chatId, loadingMsg.message_id).catch(() => {});

    if (meta.error) {
      bot.sendMessage(chatId, '❌ Could not fetch video info. Is the URL valid?');
      return;
    }

    await bot.sendPhoto(chatId, meta.thumbnail, {
      caption: `🎬 *${meta.title}*\n⏱ ${meta.duration}`,
      parse_mode: 'Markdown',
    });

    await bot.sendMessage(chatId, 'Choose format and quality:', {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '360p', callback_data: `360|${url}` },
            { text: '720p', callback_data: `720|${url}` },
            { text: '1080p', callback_data: `1080|${url}` },
          ],
          [{ text: '🎵 MP3 (audio only)', callback_data: `mp3|${url}` }],
        ],
      },
    });

    pendingDownloads.set(chatId.toString(), url);
  } catch (err: any) {
    await bot.deleteMessage(chatId, loadingMsg.message_id).catch(() => {});
    bot.sendMessage(chatId, err.message || '❌ Something went wrong.');
  }
}

async function handleSearch(chatId: number, query: string) {
  const bus = PubSubManager.getInstance();
  const loadingMsg = await bot.sendMessage(chatId, `🔍 Searching for: *${query}*...`, {
    parse_mode: 'Markdown',
  });

  try {
    const results = await bus.request('search:request', `search:response:${chatId}`, {
      query,
      chatId,
    });

    await bot.deleteMessage(chatId, loadingMsg.message_id).catch(() => {});

    if (results.error || !results.items?.length) {
      bot.sendMessage(chatId, '❌ No results found.');
      return;
    }

    const keyboard = results.items.map((item: any, i: number) => [
      {
        text: item.filesize
          ? `${(item.filesize / (1024 * 1024)).toFixed(2)} MB • ${item.title}`
          : `🎬 ${item.title} ${item.duration ? '• ' + item.duration : ''}`,
        callback_data: `result|${i}|${chatId}`,
      },
    ]);

    pendingSearchResults.set(chatId.toString(), results.items);

    await bot.sendMessage(chatId, `🗂 *Found For Your Query: ${results.query}*`, {
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: keyboard },
    });
  } catch (err: any) {
    await bot.deleteMessage(chatId, loadingMsg.message_id).catch(() => {});
    bot.sendMessage(chatId, err.message || '❌ Search failed.');
  }
}

async function main() {
  const bus = PubSubManager.getInstance();
  await bus.connect();
  console.log('Bot started, Redis connected');

  // Progress updates
  await bus.subscribe('download:progress', async (data: any) => {
    const chatId = data.chatId.toString();
    const msgId = progressMessages.get(chatId);

    try {
      if (msgId) {
        await bot.editMessageText(data.text, {
          chat_id: data.chatId,
          message_id: msgId,
        });
      } else {
        const msg = await bot.sendMessage(data.chatId, data.text);
        progressMessages.set(chatId, msg.message_id);
      }
    } catch (err) {
      // Ignore edit failures (same content)
    }
  });

  // Download done
  await bus.subscribe('download:done', async (data: any) => {
    const chatId = data.chatId.toString();
    const msgId = progressMessages.get(chatId);

    if (msgId) {
      bot.deleteMessage(data.chatId, msgId).catch(() => {});
      progressMessages.delete(chatId);
    }

    if (data.error) {
      bot.sendMessage(data.chatId, `❌ Failed: ${data.error}`);
      return;
    }

    try {
      await bot.sendMessage(data.chatId, '📤 Uploading...');

      if (data.fileType === 'audio') {
        await bot.sendAudio(data.chatId, fs.createReadStream(data.filePath));
      } else {
        await bot.sendVideo(data.chatId, fs.createReadStream(data.filePath));
      }
    } catch (err: any) {
      console.error('[BOT] Upload failed:', err.message);
      bot.sendMessage(data.chatId, '❌ Failed to upload file.');
    } finally {
      fs.unlink(data.filePath, (err) => {
        if (err) console.error('[BOT] Failed to delete file:', err);
        else console.log('[BOT] Cleaned up:', data.filePath);
      });
    }
  });

  // Message handler
  bot.on('message', async (msg) => {
    const chatId = msg.chat.id;
    const text = msg.text?.trim();

    if (!text || text.startsWith('/')) return;

    const url = text.match(urlRegex)?.[0];

    if (url) {
      await handleUrl(chatId, url);
    } else {
      await handleSearch(chatId, text);
    }
  });

  // Callback query handler
  bot.on('callback_query', async (query) => {
    if (!query.data || !query.message) return;
    await bot.answerCallbackQuery(query.id);

    const chatId = query.message.chat.id;
    const bus = PubSubManager.getInstance();

    // Quality selection (from URL flow)
    if (/^(360|720|1080)\|/.test(query.data)) {
      const [quality, url] = query.data.split('|');
      pendingDownloads.delete(chatId.toString());
      await bot.sendMessage(chatId, `⬇️ Starting download at ${quality}p...`);
      await bus.publish('download:start', { url, quality, chatId });
      return;
    }

    // MP3 selection
    if (query.data.startsWith('mp3|')) {
      const [, url] = query.data.split('|');
      pendingDownloads.delete(chatId.toString());
      await bot.sendMessage(chatId, '🎵 Starting MP3 download...');
      await bus.publish('download:start', { url, quality: 'mp3', chatId });
      return;
    }

    // Search result selected
    if (query.data.startsWith('result|')) {
      const [, indexStr, chatIdStr] = query.data.split('|');

      if (!chatIdStr) {
        bot.sendMessage(chatId, '⚠️ Session expired, please search again.');
        return;
      }

      const results = pendingSearchResults.get(chatIdStr);

      if (!results) {
        bot.sendMessage(chatId, '⚠️ Session expired, please search again.');
        return;
      }

      const item = results[parseInt(indexStr || '0', 10)];
      pendingSearchResults.delete(chatIdStr);
      pendingDownloads.set(chatId.toString(), item.url);

      if (item.thumbnail) {
        await bot.sendPhoto(chatId, item.thumbnail, {
          caption: `🎬 *${item.title}*\n⏱ ${item.duration}`,
          parse_mode: 'Markdown',
        });
      }

      await bot.sendMessage(chatId, 'Choose format and quality:', {
        reply_markup: {
          inline_keyboard: [
            [
              { text: '360p', callback_data: `360|${item.url}` },
              { text: '720p', callback_data: `720|${item.url}` },
              { text: '1080p', callback_data: `1080|${item.url}` },
            ],
            [{ text: '🎵 MP3 (audio only)', callback_data: `mp3|${item.url}` }],
          ],
        },
      });
    }
  });
}

main().catch(console.error);
