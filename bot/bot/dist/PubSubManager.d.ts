export declare class PubSubManager {
    private static instance;
    private publisher;
    private subscriber;
    private constructor();
    static getInstance(): PubSubManager;
    connect(): Promise<void>;
    publish(channel: string, data: object): Promise<void>;
    subscribe(channel: string, handler: (data: any) => void): Promise<void>;
    request(requestChannel: string, responseChannel: string, data: object, timeout?: number): Promise<any>;
    disconnect(): Promise<void>;
}
//# sourceMappingURL=PubSubManager.d.ts.map