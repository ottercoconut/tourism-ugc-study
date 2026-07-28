# 仓库可见性与许可策略

版本：1.0  
日期：2026-07-28

## 当前决定

- GitHub 仓库名：`tourism-ugc-study`
- Visibility：`Private`
- 仓库级 `LICENSE`：MIT

Private 适合当前尚在采集、标注和论文形成阶段的研究。只有仓库所有者、被明确授权的协作者，以及组织仓库中具有相应权限的成员能够访问。GitHub Free 支持私有仓库，但部分高级安全功能或 GitHub Pages 能力可能受套餐限制。

## MIT 的适用范围

本仓库同时包含四种权利属性不同的材料：

1. 研究者自行编写的软件代码；
2. 论文草稿、编码簿和研究文档；
3. 人工生成的标签、仲裁结果和派生统计；
4. 第三方平台 UGC 的文本、图片、URL 和元数据。

本项目已经选择 MIT，覆盖项目作者在仓库中原创并有权授权的代码、编码表和研究文档。MIT 允许使用、复制、修改、发布、分发和再许可，但要求保留版权与许可声明。

MIT 不自动解决四类材料之间的全部权利问题，尤其不能把第三方 UGC 重新许可为研究者自己的作品。数据由研究者采集，说明研究者建立并维护了数据集合，不当然改变原始帖子文字、图片和视频的著作权归属。由于原始 UGC 不进入仓库，它不属于本次 MIT 授权范围。

## 将来公开时的建议

| 材料 | 推荐起点 | 说明 |
| --- | --- | --- |
| 自研代码 | MIT | 当前已采用；简洁、宽松，适合公开科研代码。 |
| 原创文档、编码簿 | MIT | 为保持仓库许可简单，当前与代码使用同一许可证。 |
| 去标识化人工标签 | 单独评估 CC BY 4.0 或受控访问 | 先确认机构政策、平台条款、隐私和再识别风险。 |
| 原始 UGC、图片、作者信息 | 不随代码仓库重新许可 | 默认不公开分发；必要时只发布记录键、聚合统计或可复现抓取/派生说明。 |
| 论文稿件 | 遵循目标期刊或出版协议 | 不由代码仓库 LICENSE 统一覆盖。 |

在仓库转为 Public 前，仍需确认学校、课题组、资助方或合作单位是否对知识产权有额外规定。

## 官方依据

- [GitHub Docs：About repositories](https://docs.github.com/en/repositories/creating-and-managing-repositories/about-repositories)
- [GitHub Docs：Setting repository visibility](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
- [GitHub Docs：Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

本文件是项目管理建议，不构成法律意见。
