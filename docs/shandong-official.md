# 山东官方投档数据

`sdzk_official` 任务直接从山东省教育招生考试院（<https://www.sdzk.cn/>）导入官方 Excel 附件，
作为山东模拟志愿的核心数据。山东 2020 年起实行"专业（类）+ 学校"志愿模式、不分文理，
投档情况表本身就是"专业 + 院校"粒度的最低位次。

```bash
gaokao-vault run-spider sdzk_official                     # 2020 年至今全量
gaokao-vault run-spider sdzk_official --mode incremental  # 近 3 年
```

## 数据来源

| 栏目 | 标题 | 入库 |
|------|------|------|
| 院校投档情况（CID 47） | `山东省YYYY年普通类常规批第N次志愿投档情况表` | `provincial_admission_records` |
| 分段表（CID 49） | `YYYY年夏季高考文化(总)成绩一分一段表` | `score_segments`（科类 `综合`，取"全体"列） |

栏目列表翻页是 ASP.NET 回传，爬虫带页面隐藏字段模拟"下一页"。附件格式逐年漂移（首列空列、
两行表头、2020 年有最低分/计划数、2023 年起第 2/3 次有层次、2026 年起有选考科目要求；
2020 年一分一段表是 HTML 导出的 `.xls`），解析按表头关键字定位列，见 `spiders/sdzk.py`。

## `provincial_admission_records`

按官方表格逐行保存，唯一键为 `(province_id, year, batch, admission_round, school_code_raw, major_code_raw)`。

- `school_code_raw` 是山东本省院校代号（如 `A001`），跨年稳定，院校更名时代号不变；
  `school_id` 按校名关联 `schools`，关联不上时为空。
- 专业不强行映射到全国专业目录：大量行是专业类（`计算机类`、`理科试验班类`）。
  `major_name` 为括号前名称，`major_note` 为括号内备注，`program_type` 由备注抽取（如 `专项计划`）。
- `min_rank_raw` 保存非数字位次（如 `前50名`）。

## 往年投档推断：`gaokao_source.provincial_admission_history_v`

当年完整招生计划发布前（每年 6 月下旬），用第 1 次志愿的历年投档记录推断"今年大概率仍会招生"的
专业 + 院校：

- 按 `(院校代号, 专业名称, 备注)` 跨年汇总，`min_ranks_by_year` 给出每年最低位次，
  `latest_year` 为最近一次出现的年份，`latest_major_codes` 为最近一年的专业代号。
- 专业代号每年会变，不能用于跨年关联，也不能直接当作下一年的志愿代号。
- 同一年同校出现多个"名称 + 备注"相同但代号不同的专业（分校区、收费标准不同，表中不区分）时，
  保留当年全部位次并标记 `is_ambiguous`，不擅自取其中一个。

!!! warning "局限"
    投档表不含学费、计划数（2021 年起）、单科/体检等限制，也无法反映新增、停招和选科要求变化。
    填报期必须换成当年官方招生计划。
