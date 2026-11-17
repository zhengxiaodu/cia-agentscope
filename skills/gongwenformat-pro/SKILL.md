---
name: gongwenformat-pro
description: "党政机关公文标准排版Pro版(GB/T 9704-2012)。将Markdown/TXT/DOCX文本转换为符合国标排版的Word文档。当用户提到以下任何关键词或场景时必须使用此技能:公文排版、标准格式、Word文件、排版、写成Word、生成docx、发文格式、材料排版、红头文件、正式文件、排版成Word、帮我排个版、公文格式、公文、政府文件、机关公文、通知格式、请示格式、报告格式、函的格式、会议纪要排版、发言稿排版、研讨材料排版、汇报材料排版、红头、发文字号、版记、国家标准格式、GB/T 9704、docx排版、word标准格式、帮我做个正式文件、排版发我、转成Word、生成正式文档。也适用于用户写好内容后要求生成正式文件、发来TXT或不标准Word要求重排的场景。"
---

# 党政机关公文标准排版 Pro Skill

依据《党政机关公文格式》国家标准(GB/T 9704-2012),将任意格式文本自动排版为标准公文格式的 Word 文档。

## 何时使用

- 用户写好了研讨材料、发言稿、汇报材料、通知、请示等,需要生成正式Word文件
- 用户要求"排版"、"标准格式"、"公文格式"、"红头文件"
- 用户说"发给我Word"、"生成docx"、"帮我排个版"
- 用户发来 TXT、不标准的Word文件,要求排成标准格式

## 使用方法

### 第一步:准备输入文件

接收用户发来的文件,或将用户聊天中发的文字保存为文件。

**输入格式支持:**
- **Markdown (.md)**:自动识别 `##` 标题和 `**加粗**` 标记
- **纯文本 (.txt)**:按空行分段,智能识别层级序号
- **Word (.docx)**:提取纯文本后智能识别层级
- **直接文字**:用户在聊天中发的文字,保存为 .txt 后处理

### 第二步:生成Word文件

```bash
python3 <skill-path>/scripts/gongwen_format.py \
  --title "公文标题" \
  --input /tmp/content.md \
  --output /tmp/output.docx
```

基础参数:
- `--title`:公文标题(二号小标宋,居中)
- `--input`:输入文件路径(支持 .md / .txt / .docx)
- `--output`:输出文件路径(支持 .docx 或 .pdf)
- `--format`:输出格式 `docx`(默认)或 `pdf`(需要 LibreOffice)

查看完整参数列表:`python3 <skill-path>/scripts/gongwen_format.py --help`

### 第三步:发送文件

将生成的 `.docx` 文件发送给用户。

## 排版标准与高级参数

详细的排版标准参数(页面设置、字体字号、段落格式、页码、层次序号)、高级参数(红头、发文字号、版记、抄送等)以及注意事项,请查阅:

👉 **`references/gb9704-2012.md`** - GB/T 9704-2012 排版标准参数速查

### 常用高级参数速览

| 参数 | 说明 | 示例 |
|------|------|------|
| `--redhead` | 红头机关名称 | `--redhead "XX省人民政府"` |
| `--doc-number` | 发文字号 | `--doc-number "X政发〔2026〕12号"` |
| `--author` | 发文机关(落款) | `--author "XX镇人民政府"` |
| `--date` | 成文日期 | `--date "2026年4月16日"` |
| `--print-author` | 印发机关(版记) | `--print-author "XX镇人民政府办公室"` |
| `--print-date` | 印发日期(版记) | `--print-date "2026-04-17"` |
| `--cc` | 抄送机关(版记) | `--cc "县委办公室"` |
| `--copies` | 份号(6位数字) | `--copies "000001"` |
| `--secret-level` | 密级和保密期限 | `--secret-level "机密★20年"` |
| `--urgency` | 紧急程度 | `--urgency "特急"` |
| `--signer` | 签发人(上行文) | `--signer "张三"` |
| `--recipient` | 主送机关 | `--recipient "县委办公室"` |
| `--notes` | 附注(可多个) | `--notes "此件公开发布"` |

## 快速示例

用户说：“帮我把这篇研讨材料排成标准格式发给我”

操作步骤：
1. 将用户发来的内容保存为文件（md/txt/docx），或接收用户发送的文件
2. 运行 gongwen_format.py 生成 Word
3. 发送 .docx 文件给用户

### 完整示例（包含所有新要素）

用户说：“帮我排一份涉密公文，要包含份号、密级、签发人、主送机关、附注”

```bash
python3 <skill-path>/scripts/gongwen_format.py \
  --title “关于进一步加强党建工作的通知” \
  --input /tmp/content.md \
  --output /tmp/output.docx \
  --redhead “XX省人民政府” \
  --doc-number “X政发〔2026〕12号” \
  --copies “000001” \
  --secret-level “机密★20年” \
  --urgency “特急” \
  --signer “张三” \
  --recipient “各市、县人民政府，省直各部门：” \
  --author “XX省人民政府” \
  --date “2026年4月26日” \
  --notes “此件公开发布” “联系人：李四，电话：0571-88888888” \
  --print-author “XX省人民政府办公室” \
  --print-date “2026-04-27” \
  --cc “省委办公厅，省人大常委会办公厅，省政协办公厅”
```

### PDF 导出示例

用户说:"帮我排版,要 PDF 格式"

```bash
python3 <skill-path>/scripts/gongwen_format.py \
  --title "关于XXX的实施意见" \
  --input /tmp/content.md \
  --output /tmp/output.pdf
```

也可以用 `--format pdf` 参数:

```bash
python3 <skill-path>/scripts/gongwen_format.py \
  --title "关于XXX的实施意见" \
  --input /tmp/content.md \
  --output /tmp/output.docx \
  --format pdf
```

> ⚠️ PDF 导出需要安装 LibreOffice:`sudo apt install libreoffice-writer`
