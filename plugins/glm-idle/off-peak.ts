const hourMs = 60 * 60 * 1000;
const dayMs = 24 * hourMs;
const peakStartHour = 6;
const peakEndHour = 10;

export function offPeakStatus(now: number) {
  const time = new Date(now);
  const weekDay = time.getUTCDay();
  const hour = time.getUTCHours();
  const weekday = weekDay >= 1 && weekDay <= 5;
  const dayStart = Math.floor(now / dayMs) * dayMs;
  if (weekday && hour >= peakStartHour && hour < peakEndHour) {
    return { offPeak: false, nextChangeAt: new Date(dayStart + peakEndHour * hourMs).toISOString() };
  }
  const daysToStart = weekday && hour < peakStartHour ? 0
    : weekDay === 5 ? 3 : weekDay === 6 ? 2 : 1;
  return {
    offPeak: true,
    nextChangeAt: new Date(dayStart + daysToStart * dayMs + peakStartHour * hourMs).toISOString(),
  };
}
