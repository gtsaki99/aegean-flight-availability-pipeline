"""Sanity report on the Task 2 output and the AA / US / AA_GROUP chart."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# Categorical palette checked for colour-vision deficiency (all pairs distinguishable); light background.
SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = {"AA": "#2a78d6", "US": "#eb6834", "AA_GROUP": "#1baf7a"}


def md_table(frame, floatfmt=None):
    """Minimal Markdown table (no extra dependency); floats keep the precision given in SQL."""
    def cell(v):
        if isinstance(v, float):
            if v != v:
                return ""
            return f"{int(v):,}" if v.is_integer() else f"{v:,}"
        if isinstance(v, int):
            return f"{v:,}"
        return str(v)
    header = "| " + " | ".join(frame.columns) + " |"
    rule = "|" + "|".join("---" for _ in frame.columns) + "|"
    rows = ["| " + " | ".join(cell(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join([header, rule, *rows])


def sanity_report(con):
    q = lambda sql: con.sql(sql).df()  # noqa: E731
    lines = ["# Task 2 sanity report", "", "Checks on `flights_availability` (minutes; one row per airline and local day).", ""]

    rows, days, airlines = con.sql("select count(*), count(distinct flight_date), count(distinct airline) "
                                   "from flights_availability").fetchone()
    lines += [f"- Rows: {rows:,} ({days} days, {airlines} airlines)", ""]

    inv = con.sql("""select count(*) filter (where night_availability > availability),
                            count(*) filter (where availability > n_aircraft * 1500),
                            count(*) filter (where idle_day_availability > n_aircraft),
                            count(*) filter (where n_aircraft = 0)
                     from flights_availability""").fetchone()
    lines += ["## Invariants", "",
              md_table(pd.DataFrame({"Rule": ["night_availability ≤ availability", "availability ≤ n_aircraft × 25 h",
                                              "idle_day_availability ≤ n_aircraft", "n_aircraft > 0"],
                                     "Rows breaking it": list(inv)})), ""]

    lines += ["## Rows per airline", "",
              md_table(q("""select airline "Airline", count(*) "Days", min(flight_date)::varchar "First",
                            max(flight_date)::varchar "Last" from flights_availability group by 1 order by 1""")), "",
              "US stops after 2015-06-30 (reported under AA from 2015-07-01). Its row on 2015-07-01 is ground time "
              "between US flights scheduled on Jun 30 but delayed past midnight.", ""]

    lines += ["## Per airline (2015 averages)", "",
              md_table(q("""select airline "Airline", round(avg(n_aircraft)) "Aircraft per day",
                            round(sum(availability) / sum(n_aircraft) / 60, 1) "Ground h per aircraft-day",
                            round(sum(night_availability) / sum(n_aircraft) / 60, 1) "Night ground h (of 8)",
                            round(avg(idle_day_availability), 1) "Idle aircraft per day",
                            round(100.0 * sum(idle_day_availability) / sum(n_aircraft), 1) "% aircraft-days idle"
                            from flights_availability group by 1 order by 3, 1""")), "",
              "Low-cost airlines have the least ground time; network airlines the most, and the most idle aircraft-days, "
              "which fits the documented limitation (their international flights are not in the dataset).", ""]

    lines += ["## Key days (all airlines)", "",
              md_table(q("""select flight_date::varchar "Day",
                            case flight_date when '2015-01-27' then 'winter storm (19% cancelled)'
                                 when '2015-03-07' then 'day before DST' when '2015-03-08' then 'DST start: 23 h day, 7 h night'
                                 when '2015-03-09' then 'day after DST' when '2015-06-30' then 'last US day'
                                 when '2015-07-01' then 'US reported under AA' when '2015-11-01' then 'DST end: 25 h day, 9 h night'
                                 when '2015-11-26' then 'Thanksgiving' end "Why",
                            sum(n_aircraft) "Aircraft", round(sum(availability) / 60) "Ground h", round(sum(night_availability) / 60) "Night h",
                            sum(idle_day_availability) "Idle aircraft", sum(n_cancelled) "Cancelled"
                            from flights_availability
                            where flight_date in ('2015-01-27', '2015-03-07', '2015-03-08', '2015-03-09', '2015-06-30',
                                                  '2015-07-01', '2015-11-01', '2015-11-26')
                            group by 1, 2 order by 1""")), "",
              f"Typical day: {con.sql('select round(median(i)) from (select sum(idle_day_availability) i from flights_availability group by flight_date)').fetchone()[0]:.0f} idle aircraft (median over 2015).", ""]

    lines += ["## Edge of the data", "",
              md_table(q("""with d as (select flight_date, sum(n_aircraft) n, sum(idle_day_availability) idle
                                       from flights_availability group by 1),
                            m as (select median(n) mn from d where flight_date between '2015-02-01' and '2015-11-30')
                            select flight_date::varchar "Day", n "Aircraft counted", round(100.0 * n / mn) "% of typical",
                                   idle "Idle aircraft" from d, m
                            where flight_date in ('2015-01-01', '2015-01-07', '2015-01-14', '2015-12-18', '2015-12-24', '2015-12-31')
                            order by 1""")), "",
              "An aircraft only counts from its first to its last recorded flight (rule C), so the first and last ~3 weeks "
              "of 2015 undercount the fleet (by up to 16% on Jan 1 and Dec 31). In the daily pipeline the start effect doesn't "
              "exist (history is loaded); the end effect is why recent days are provisional.", ""]

    est = con.sql("select sum(estimated_minutes), sum(availability) from flights_availability").fetchone()
    lines += ["## Estimated flying time", "",
              f"- Flying minutes that are estimated (diverted without arrival, cancelled after take-off): {est[0]:,} "
              f"({est[0] / est[1]:.3%} of total ground time)", ""]
    return "\n".join(lines) + "\n"


def aa_us_chart(con, path):
    """Daily ground time of AA and US as reported, and combined as AA_GROUP."""
    df = con.sql("""select flight_date, airline, availability / 60000.0 as k_hours from flights_availability
                    where airline in ('AA', 'US') order by 1""").df()
    wide = df.pivot(index="flight_date", columns="airline", values="k_hours")
    wide.index = pd.to_datetime(wide.index)
    wide["US"] = wide["US"].where(wide.index <= "2015-06-30")      # the Jul 1 remnant is a few minutes
    group = wide[["AA", "US"]].fillna(0).sum(axis=1)

    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Segoe UI", "DejaVu Sans"],
                         "font.size": 9, "text.color": INK_2, "axes.labelcolor": INK_2,
                         "xtick.color": MUTED, "ytick.color": MUTED})
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True, sharey=True, facecolor=SURFACE)
    panels = [("As reported", [("AA", wide["AA"]), ("US", wide["US"])]),
              ("Combined: AA_GROUP = AA + US", [("AA_GROUP", group)])]
    switch = pd.Timestamp("2015-07-01")
    for ax, (title, series) in zip(axes, panels):
        ax.set_facecolor(SURFACE)
        for name, values in series:
            ax.plot(values.index, values.values, color=SERIES[name], linewidth=1.6, label=name, solid_capstyle="round")
            last = values.dropna()
            ax.plot(last.index[-1], last.values[-1], "o", color=SERIES[name], markersize=4)
            ax.annotate(name, (last.index[-1], last.values[-1]), xytext=(6, 0), textcoords="offset points",
                        va="center", color=INK_2, fontsize=9)
        ax.axvline(switch, color=MUTED, linewidth=0.8)
        ax.set_title(title, loc="left", color=INK, fontsize=10, pad=6)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(length=0)
        ax.set_ylabel("Ground time\n(thousand aircraft-hours)")
        if len(series) > 1:
            ax.legend(loc="upper left", frameon=False, ncols=len(series))
    axes[0].annotate("Jul 1: US flights reported under AA", (switch, 1.0), xycoords=("data", "axes fraction"),
                     xytext=(5, -2), textcoords="offset points", va="top", color=INK_2, fontsize=8.5)
    axes[1].xaxis.set_major_locator(mdates.MonthLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    axes[1].set_ylim(bottom=0)
    fig.suptitle("American Airlines and US Airways: daily aircraft ground time, 2015", x=0.012, ha="left",
                 color=INK, fontsize=12, fontweight="bold")
    fig.text(0.012, 0.028, "Source: BTS 2015 on-time data (Kaggle). Ground time per local day, all aircraft of the airline "
             "(flights_availability.availability).", color=MUTED, fontsize=7.5)
    fig.text(0.012, 0.008, "Early January and late December are lower because an aircraft only counts from its first to its "
             "last recorded 2015 flight (edge of the data).", color=MUTED, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.05, 0.97, 0.96))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return path
