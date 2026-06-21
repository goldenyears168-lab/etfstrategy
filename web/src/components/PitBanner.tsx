interface Props {
  date: string;
  syncedAt?: string | null;
}

export function PitBanner({ date, syncedAt }: Props) {
  const synced = syncedAt
    ? new Date(syncedAt).toLocaleString("zh-TW", { hour12: false })
    : null;
  return (
    <div className="pit-banner">
      PIT 訊號日 {date} · 僅使用 date ≤ {date} 的資料 · 非即時交易建議
      {synced && ` · 同步 ${synced}`}
    </div>
  );
}
