# 材料构建脚本

> 这些脚本**不是运行时**，也不进 `tools/file_fingerprint` 的同步清单；
> 只用来重建技术方案、答辩 PPT、产品设计文档。
>
> 从**仓库根目录**运行；路径按仓库根解析，不依赖当前目录。

## 依赖

```bash
# 文档/PPT 构建（与运行时 requirements.txt 分开）
python -m pip install -r docs/build/requirements.txt
```

还需要：

- **Pandoc**：Markdown → DOCX；
- **LibreOffice**（或 LibreOffice Kit）：DOCX → PDF；
- 若用本项目的 bundled Python，则上面两个 Python 包通常已带。

## 命令

```bash
# 技术方案 DOCX（内部会调 Pandoc / LibreOffice）
python docs/build/build_technical_report.py

# 答辩 PPT（16:9，15 页主 deck + 1 页 Q&A 备用）
python docs/build/build_tech_ppt.py

# 产品设计 DOCX：先 Pandoc 生成，再套样式/页眉页脚
pandoc docs/product/产品形态与界面设计.md \
  -o docs/product/产品形态与界面设计.docx \
  --reference-doc=docs/aicomp/技术方案-法前哨.docx \
  --resource-path=docs/product --from=markdown+pipe_tables
python docs/build/style_design_doc.py
python docs/build/fix_design_headers.py

# 隐藏牌/手机端设计 DOCX
pandoc docs/product/隐藏牌与手机端设计.md \
  -o docs/product/隐藏牌与手机端设计.docx \
  --reference-doc=docs/aicomp/技术方案-法前哨.docx \
  --resource-path=docs/product --from=markdown+pipe_tables
python docs/build/style_mobile_doc.py
python docs/build/fix_design_headers.py
```

> `build_technical_report.py` / `build_tech_ppt.py` 会覆盖 `docs/aicomp/` 下的成品；
> 先确认当前版本已备份或提交。
>
> 构建脚本不参与运行时自检；改完只需确认：
> `python -m compileall -q docs/build` 通过，且生成物能被打开。
