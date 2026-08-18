"use strict";

document.querySelectorAll("time[data-relative-time]").forEach((element) => {
  const instant = new Date(element.dateTime);
  if (Number.isNaN(instant.getTime())) return;
  const seconds = Math.round((instant.getTime() - Date.now()) / 1000);
  const divisions = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
    [4.345, "week"],
    [12, "month"],
    [Number.POSITIVE_INFINITY, "year"],
  ];
  let value = seconds;
  let unit = "second";
  for (const [amount, candidate] of divisions) {
    unit = candidate;
    if (Math.abs(value) < amount) break;
    value /= amount;
  }
  element.textContent = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    Math.round(value),
    unit,
  );
});
