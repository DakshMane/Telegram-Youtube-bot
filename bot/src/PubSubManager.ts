import { RedisClientType, createClient } from 'redis';

export class PubSubManager {
  private static instance: PubSubManager;
  private publisher: RedisClientType;
  private subscriber: RedisClientType;

  private constructor() {
    const url = process.env.REDIS_URL || 'redis://localhost:6379';
    this.publisher = createClient({ url });
    this.subscriber = createClient({ url });
  }

  static getInstance(): PubSubManager {
    if (!PubSubManager.instance) {
      PubSubManager.instance = new PubSubManager();
    }
    return PubSubManager.instance;
  }

  async connect() {
    await this.publisher.connect();
    await this.subscriber.connect();
    console.log('Redis connected');
  }

  async publish(channel: string, data: object) {
    await this.publisher.publish(channel, JSON.stringify(data));
  }

  async subscribe(channel: string, handler: (data: any) => void) {
    await this.subscriber.subscribe(channel, (message) => {
      handler(JSON.parse(message));
    });
  }

  async request(
    requestChannel: string,
    responseChannel: string,
    data: object,
    timeout = 60000
  ): Promise<any> {
    const url = process.env.REDIS_URL || 'redis://localhost:6379';
    const tempClient: RedisClientType = createClient({ url }) as RedisClientType;
    await tempClient.connect();

    return new Promise((resolve, reject) => {
      const timer = setTimeout(async () => {
        await tempClient.unsubscribe(responseChannel);
        await tempClient.quit();
        reject(new Error('⏱ Request timed out, try again.'));
      }, timeout);

      tempClient
        .subscribe(responseChannel, async (message) => {
          clearTimeout(timer);
          await tempClient.unsubscribe(responseChannel);
          await tempClient.quit();
          resolve(JSON.parse(message));
        })
        .then(() => {
          this.publish(requestChannel, data).catch(async (err) => {
            clearTimeout(timer);
            await tempClient.quit();
            reject(err);
          });
        })
        .catch(async (err) => {
          clearTimeout(timer);
          await tempClient.quit();
          reject(err);
        });
    });
  }

  async disconnect() {
    await this.publisher.quit();
    await this.subscriber.quit();
  }
}
