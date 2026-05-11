# 汉口（武汉关）长江水位 Plotly 可视化

这个仓库每天自动抓取长江航道局水位页面中的“汉口（武汉关）”水位，保存 08:00 和 16:00 两个时次，并生成可缩放、可拖拽调整大小的 Plotly 网页。数据会长期累积，页面默认显示近 60 天，也可以切换到 1周、1月、3个月、6个月、1年、全部或自定义 N 年。

数据来源：<https://www.cjhdj.com.cn/hdfw/sw/>

## 自动运行时间

GitHub Actions 会在北京时间每天 09:20 和 17:20 运行一次：

- 09:20 抓取当天 08:00 水位。
- 17:20 抓取当天 16:00 水位。
- 也可以在 Actions 页面手动运行 `Update Hankou water level chart`。

## 输出文件

- `data/hankou_water_levels.csv`：长期累积数据。
- `docs/index.html`：GitHub Pages 展示页。
- `docs/hankou_water_levels.csv`：页面可下载的全部累积 CSV。
- `docs/hankou_water_levels.json`：页面使用的全部累积结构化数据。

## GitHub Pages 链接

部署成功后，访问：

`https://<你的 GitHub 用户名>.github.io/<仓库名>/`

如果使用账号 `jyxie2025` 并创建仓库 `hankou-water-level`，链接就是：

`https://jyxie2025.github.io/hankou-water-level/`

## 说明

长江航道局当前水位栏目公开页面主要展示最近发布的水位文章，不提供可直接下载的 60 天历史接口。这个项目会把每天抓到的记录持续累积；如果后续补充历史 CSV，脚本会自动与现有数据合并。
