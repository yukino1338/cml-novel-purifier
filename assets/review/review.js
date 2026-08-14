/* cml-review-template:__CML_REVIEW_TEMPLATE_VERSION__ */
(() => {
  "use strict";

  const VERDICTS = new Set(["delete", "keep", "uncertain"]);
  const REASONS = new Set([
    "external_ad_block", "narrative_text", "mixed_keep", "insufficient_context",
    "inconsistent_occurrences", "segment_boundary_wrong", "custom",
  ]);
  const REASON_LABELS = {
    external_ad_block: "外部广告块", narrative_text: "剧情正文", mixed_keep: "混合内容需保留",
    insufficient_context: "上下文不足", inconsistent_occurrences: "多处内容不一致",
    segment_boundary_wrong: "子段边界不正确", custom: "其他原因",
  };
  const VERDICT_LABELS = {delete: "删除", keep: "保留", uncertain: "暂不判断"};
  const FORMAL_LABELS = {delete: "删除", keep: "保留", uncertain: "未决"};
  const DRAFT_LABELS = {delete: "删除", keep: "保留", uncertain: "待判断"};
  const escapeHtml = (value) => String(value ?? "").replace(
    /[&<>'"]/g,
    (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[character]),
  );
  const jsonText = (value) => {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return "";
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  };

  const copyText = async (value) => {
    const text = String(value);
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        // Local file pages commonly lack clipboard permission.
      }
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.readOnly = true;
    area.className = "copy-fallback";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("copy failed");
  };

  document.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const copyButton = target.closest("[data-copy-command]");
    if (!copyButton) return;
    const row = copyButton.closest(".command-row");
    const command = row?.querySelector("[data-command-text]")?.textContent || "";
    const status = row?.querySelector(".copy-status");
    try {
      await copyText(command);
      if (status) status.textContent = "命令已复制。";
    } catch {
      if (status) status.textContent = "复制失败，请手动选择命令文本。";
    }
  });

  document.querySelectorAll(".exception-review").forEach((root, rootIndex) => {
    const payloadNode = root.querySelector(".review-payload");
    let payload;
    try {
      payload = JSON.parse(payloadNode?.textContent || "null");
    } catch {
      root.replaceChildren(Object.assign(document.createElement("p"), {textContent: "复核数据损坏，页面已安全停止。"}));
      return;
    }
    if (!payload || payload.review_ui_schema !== 2 || typeof payload.review_state_id !== "string") {
      root.replaceChildren(Object.assign(document.createElement("p"), {textContent: "复核数据版本不受支持，页面已安全停止。"}));
      return;
    }

    const items = Object.entries(payload.modules || {}).flatMap(([module, value]) =>
      Array.isArray(value?.items) ? value.items.map((item) => ({...item, module})) : []
    );
    const itemById = new Map(items.map((item) => [String(item.candidate_id), item]));
    const moduleInput = root.querySelector("[data-review-module]");
    const scopeInput = root.querySelector("[data-review-scope]");
    const statusInput = root.querySelector("[data-review-status]");
    const searchInput = root.querySelector("[data-review-search]");
    const countNode = root.querySelector("[data-review-count]");
    const selectedNode = root.querySelector("[data-review-selected]");
    const resultsNode = root.querySelector("[data-review-results]");
    const copyStatus = root.querySelector("[data-review-copy-status]");
    const batchStatus = root.querySelector("[data-review-batch-status]");
    const batchNoteInput = root.querySelector("[data-review-batch-note]");
    const stateKey = `cml-novel-purifier:review:v2:${payload.review_state_id}`;
    const pageSize = 25;

    const firstActionable = items.find((item) => item.needs_review || !item.formal_decision) || items[0];
    let state = {
      schema_version: 2,
      review_state_id: payload.review_state_id,
      filters: {module: "all", scope: items.some((item) => item.needs_review) ? "review" : "all", status: "all", search: "", scroll_y: 0},
      page: 1,
      active_candidate_id: firstActionable?.candidate_id || "",
      decisions: {},
      expanded_technical_ids: [],
      checked_ids: [],
    };

    const cleanState = (candidate) => {
      if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return null;
      const stateKeys = ["schema_version", "review_state_id", "filters", "page", "active_candidate_id", "decisions", "expanded_technical_ids", "checked_ids"];
      if (Object.keys(candidate).some((key) => !stateKeys.includes(key)) || stateKeys.some((key) => !(key in candidate))) return null;
      if (candidate.schema_version !== 2 || candidate.review_state_id !== payload.review_state_id) return null;
      if (!candidate.filters || typeof candidate.filters !== "object" || Array.isArray(candidate.filters)) return null;
      const filterKeys = ["module", "scope", "status", "search", "scroll_y"];
      if (Object.keys(candidate.filters).some((key) => !filterKeys.includes(key))) return null;
      const allowedStatuses = new Set(["all", "no-formal", "formal:uncertain", "formal:delete", "formal:keep", "draft:uncertain", "draft:delete", "draft:keep"]);
      if (!allowedStatuses.has(candidate.filters.status)) return null;
      const filters = {
        module: ["all", "ads", "titles", "blocked"].includes(candidate.filters.module) ? candidate.filters.module : "all",
        scope: ["review", "all"].includes(candidate.filters.scope) ? candidate.filters.scope : "review",
        status: typeof candidate.filters.status === "string" ? candidate.filters.status : "all",
        search: typeof candidate.filters.search === "string" && candidate.filters.search.length <= 200 ? candidate.filters.search : "",
        scroll_y: Number.isFinite(candidate.filters.scroll_y) && candidate.filters.scroll_y >= 0 ? candidate.filters.scroll_y : 0,
      };
      const active = String(candidate.active_candidate_id || "");
      if (active && !itemById.has(active)) return null;
      const decisions = {};
      if (!candidate.decisions || typeof candidate.decisions !== "object" || Array.isArray(candidate.decisions)) return null;
      for (const [candidateId, decision] of Object.entries(candidate.decisions)) {
        if (!itemById.has(candidateId) || !decision || typeof decision !== "object" || Array.isArray(decision)) return null;
        const verdict = decision.verdict;
        const reasonCode = decision.reason_code;
        const note = decision.note;
        if (Object.keys(decision).some((key) => !["verdict", "reason_code", "note"].includes(key))) return null;
        if (!VERDICTS.has(verdict) || !REASONS.has(reasonCode) || typeof note !== "string" || note.length > 500) return null;
        decisions[candidateId] = {verdict, reason_code: reasonCode, note};
      }
      const cleanIds = (value) => {
        if (!Array.isArray(value)) return null;
        const ids = [...new Set(value.map(String))];
        return ids.every((candidateId) => itemById.has(candidateId)) ? ids : null;
      };
      const expanded = cleanIds(candidate.expanded_technical_ids);
      const checked = cleanIds(candidate.checked_ids);
      if (!expanded || !checked) return null;
      return {
        schema_version: 2,
        review_state_id: payload.review_state_id,
        filters,
        page: Number.isInteger(candidate.page) && candidate.page > 0 ? candidate.page : 1,
        active_candidate_id: active || firstActionable?.candidate_id || "",
        decisions,
        expanded_technical_ids: expanded,
        checked_ids: checked,
      };
    };

    try {
      const saved = JSON.parse(sessionStorage.getItem(stateKey) || "null");
      state = cleanState(saved) || state;
    } catch {
      // Corrupt local state is intentionally ignored.
    }

    moduleInput.value = state.filters.module;
    scopeInput.value = state.filters.scope;
    statusInput.value = state.filters.status;
    searchInput.value = state.filters.search;

    const saveState = ({preserveScroll = false} = {}) => {
      state.filters = {
        module: moduleInput.value,
        scope: scopeInput.value,
        status: statusInput.value,
        search: searchInput.value.slice(0, 200),
        scroll_y: preserveScroll ? state.filters.scroll_y : Math.max(0, window.scrollY),
      };
      root.dataset.reviewState = JSON.stringify(state);
      try {
        sessionStorage.setItem(stateKey, JSON.stringify(state));
      } catch {
        // The in-page state remains usable if browser storage is unavailable.
      }
    };

    const matchesStatus = (item) => {
      const selected = statusInput.value;
      if (selected === "all") return true;
      if (selected === "no-formal") return !item.formal_decision;
      const [source, verdict] = selected.split(":");
      return source === "formal" ? item.formal_decision === verdict : item.draft_verdict === verdict;
    };
    const filteredItems = () => {
      const keyword = searchInput.value.trim().toLocaleLowerCase("zh-CN");
      return items.filter((item) => {
        if (moduleInput.value !== "all" && item.module !== moduleInput.value) return false;
        if (scopeInput.value === "review" && !item.needs_review) return false;
        if (!matchesStatus(item)) return false;
        if (!keyword) return true;
        const haystack = [item.display_title, item.plain_reason, item.original, item.before, item.after, item.chapter?.title]
          .map((value) => String(value ?? "").toLocaleLowerCase("zh-CN")).join("\n");
        return haystack.includes(keyword);
      });
    };

    const locationText = (item) => {
      const chapter = item.chapter;
      const chapterText = chapter?.title
        ? `${Number.isInteger(chapter.index) ? `第 ${chapter.index} 章 ` : ""}${chapter.title}`
        : Number.isInteger(chapter?.index) ? `第 ${chapter.index} 章` : "定位块";
      const line = Number.isInteger(item.line_number) ? `第 ${item.line_number} 行` : "行号未知";
      return `${chapterText} · ${line} · 共 ${Number(item.occurrence_count ?? item.anchors_count ?? 0)} 处`;
    };

    const highlightedText = (item) => {
      const original = String(item.original ?? "");
      const match = String(item.match_text ?? "");
      const index = match ? original.indexOf(match) : -1;
      if (index < 0) return escapeHtml(original);
      return `${escapeHtml(original.slice(0, index))}<mark>${escapeHtml(match)}</mark>${escapeHtml(original.slice(index + match.length))}`;
    };

    const technicalRows = (item) => {
      const rows = [
        ["候选追踪号", item.candidate_id], ["家族追踪号", item.cluster_id],
        ["家族签名", item.family_signature], ["精确锚点", item.anchors],
        ["判定证据", item.evidence], ["升级来源", item.promoted_from],
        ["相邻证据", item.neighbor_span], ["正式结论依据", item.formal_reason],
        ["系统建议依据", item.draft_reason], ["安全阻止项", item.delete_blockers],
      ].filter(([, value]) => jsonText(value));
      return rows.length
        ? `<dl class='evidence-list'>${rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd><pre>${escapeHtml(jsonText(value))}</pre></dd>`).join("")}</dl>`
        : "<p class='empty-state'>没有额外技术信息。</p>";
    };

    const decisionFor = (item) => state.decisions[item.candidate_id] || null;
    const defaultReason = (verdict) => verdict === "delete" ? "external_ad_block" : verdict === "keep" ? "narrative_text" : "insufficient_context";
    const decisionError = (item, decision) => {
      if (!decision) return "";
      if (decision.verdict === "delete" && !item.delete_allowed && !item.segment_delete_allowed) return "当前候选不具备安全删除资格。";
      if (decision.reason_code === "segment_boundary_wrong" && decision.verdict !== "uncertain") return "子段边界不正确时必须选择暂不判断。";
      const overridesFormal = item.formal_decision && item.formal_decision !== decision.verdict;
      if ((decision.verdict === "uncertain" || overridesFormal || decision.reason_code === "custom" || decision.reason_code === "segment_boundary_wrong") && !decision.note.trim()) {
        return "此选择必须填写备注。";
      }
      return "";
    };

    const renderQueue = (visible) => {
      const groups = [];
      const byGroup = new Map();
      visible.forEach((item) => {
        const key = item.review_group_id || item.candidate_id;
        if (!byGroup.has(key)) {
          const group = {id: key, label: item.family_label || "候选", items: []};
          byGroup.set(key, group);
          groups.push(group);
        }
        byGroup.get(key).items.push(item);
      });
      return groups.map((group) => `
        <section class='queue-group' aria-label='${escapeHtml(group.label)}'>
          <div class='queue-group-head'><span>${escapeHtml(group.label)}</span><button type='button' class='text-button' data-check-group='${escapeHtml(group.id)}'>勾选本组</button></div>
          <ol>${group.items.map((item) => {
            const decision = decisionFor(item);
            const active = item.candidate_id === state.active_candidate_id;
            return `<li class='queue-item${active ? " is-active" : ""}' data-queue-candidate='${escapeHtml(item.candidate_id)}'>
              <label class='queue-check'><input type='checkbox' data-review-check='${escapeHtml(item.candidate_id)}' ${state.checked_ids.includes(item.candidate_id) ? "checked" : ""}><span class='sr-only'>勾选候选 ${escapeHtml(item.display_index)}</span></label>
              <button type='button' class='queue-open' data-review-open='${escapeHtml(item.candidate_id)}' ${active ? "aria-current='true'" : ""}>
                <b>候选 ${escapeHtml(item.display_index)} · ${escapeHtml(item.display_title)}</b>
                <span>${escapeHtml(locationText(item))}</span>
                <small data-queue-decision>${decision ? `请求${VERDICT_LABELS[decision.verdict]}` : item.formal_decision ? `正式${FORMAL_LABELS[item.formal_decision]}` : "待复核"}</small>
              </button>
            </li>`;
          }).join("")}</ol>
        </section>`).join("");
    };

    const renderActive = (item) => {
      if (!item) return "<section class='review-current empty-state'>没有符合当前条件的候选。</section>";
      const decision = decisionFor(item);
      const original = String(item.original ?? "");
      const isLong = original.length > 220 || original.split("\n").length > 5;
      const textId = `review-text-${rootIndex}-${item.display_index}`;
      const technicalOpen = state.expanded_technical_ids.includes(item.candidate_id) ? " open" : "";
      const mixedCandidate = item.segment_delete_allowed || item.mutation_guard === "long_line_mixed_content" || item.mutation_guard === "segment_review_required";
      const deleteLabel = item.segment_delete_allowed ? "只删除标出的广告片段" : "删除整块";
      const deleteDisabled = !item.delete_allowed && !item.segment_delete_allowed;
      const segmentPreviews = Array.isArray(item.segment_previews) ? item.segment_previews : [];
      const segmentBoundaryLabels = {
        external_prefix: "开头的外部引导",
        external_suffix: "结尾的外部引导",
        standalone_clause: "独立的外部引导句",
      };
      const segmentPreviewList = segmentPreviews.length
        ? `<ol class='segment-preview-list'>${segmentPreviews.map((preview, previewIndex) => `
            <li><details class='segment-preview'><summary>第 ${previewIndex + 1} / ${segmentPreviews.length} 处 · ${escapeHtml(segmentBoundaryLabels[preview.boundary_kind] || "已锁定边界")}</summary>
              <dl><dt>保留</dt><dd>${escapeHtml(preview.keep_text || "")}</dd><dt>删除</dt><dd>${escapeHtml(preview.delete_text || "")}</dd><dt>删除后</dt><dd>${escapeHtml(preview.after_text || "")}</dd></dl>
              ${preview.preview_truncated ? "<p class='bounded-notice'>此处预览已截断；必须以当前工作区和 Agent dry-run 为准。</p>" : ""}
            </details></li>`).join("")}</ol>`
        : "<p class='bounded-notice'>当前页面没有完整的逐处预览；不能据此直接形成删除请求。</p>";
      const reasonOptions = [...REASONS].map((code) => `<option value='${code}' ${decision?.reason_code === code ? "selected" : ""}>${escapeHtml(REASON_LABELS[code])}</option>`).join("");
      const formal = item.formal_decision
        ? `<div class='decision-layer formal-layer'><span>正式结论</span><b>${escapeHtml(FORMAL_LABELS[item.formal_decision] || item.formal_decision)}</b></div>`
        : `<div class='decision-layer formal-layer'><span>正式结论</span><b>尚未形成</b></div>`;
      const draft = item.draft_verdict
        ? `<div class='decision-layer draft-layer'><span>系统曾建议</span><b>${escapeHtml(DRAFT_LABELS[item.draft_verdict] || item.draft_verdict)}</b></div>`
        : "";
      const proposal = `<div class='decision-layer proposal-layer' data-proposal-layer><span>尚未应用的复核请求</span><b data-proposal-value>${decision ? VERDICT_LABELS[decision.verdict] : "未选择"}</b></div>`;
      const mixedBrief = mixedCandidate
        ? `<p class='mixed-brief'><b>正文与广告混合：</b>${escapeHtml(item.segment_support_message)}${item.segment_delete_allowed ? ` 共 ${segmentPreviews.length} 处可审计片段；删除前请逐项核对。` : ""}</p>`
        : "";
      const mixedDetails = mixedCandidate
        ? `<section class='mixed-panel'><b>正文与广告混合</b><p>${escapeHtml(item.segment_support_message)}</p>${item.segment_delete_allowed ? `<p>本候选共有 ${segmentPreviews.length} 处可审计片段；请逐项展开确认保留、删除和删除后文本。</p>${segmentPreviewList}${item.segment_previews_truncated ? "<p class='bounded-notice'>存在截断的逐处预览，Agent 必须重读完整工作区后才可应用。</p>" : ""}` : ""}</section>`
        : "";
      const excerptNotice = item.excerpt_truncated || item.anchors_truncated || item.metadata_truncated
        ? "<p class='bounded-notice'>网页仅展示有界摘录；完整内容以工作区工件为准。</p>" : "";
      return `<article class='review-card review-current' data-active-candidate='${escapeHtml(item.candidate_id)}'>
        <header class='review-card-head'><div><p class='eyebrow'>候选 ${escapeHtml(item.display_index)}</p><h3 tabindex='-1' data-review-active-heading>${escapeHtml(item.display_title)}</h3></div><span class='module-chip'>${escapeHtml(payload.modules[item.module]?.label || item.module)}</span></header>
        <p class='review-chapter'>${escapeHtml(locationText(item))}</p>
        <p class='plain-reason'>${escapeHtml(item.plain_reason)}</p>
        <div class='candidate-body'>
          <div class='candidate-evidence'>
            <div class='review-text'><pre class='review-original${isLong ? " is-collapsed" : ""}' id='${textId}'>${highlightedText(item)}</pre></div>
            ${excerptNotice}
          </div>
          <div class='candidate-decision'>
            <div class='decision-layers'>${formal}${draft}${proposal}</div>${mixedBrief}
            <fieldset class='verdict-fieldset'><legend>这条复核请求</legend>${["delete", "keep", "uncertain"].map((verdict) => {
              const disabled = verdict === "delete" && deleteDisabled;
              const verdictLabel = verdict === "delete" ? deleteLabel : verdict === "keep" && mixedCandidate ? "保留整段" : VERDICT_LABELS[verdict];
              return `<label class='radio-choice choice-${verdict}${disabled ? " is-disabled" : ""}'><input type='radio' name='verdict-${rootIndex}' value='${verdict}' data-review-choice='${verdict}' data-candidate-id='${escapeHtml(item.candidate_id)}' ${decision?.verdict === verdict ? "checked" : ""} ${disabled ? "disabled" : ""}><span>${escapeHtml(verdictLabel)}</span></label>`;
            }).join("")}</fieldset>
            ${deleteDisabled ? `<p class='delete-disabled-note'>不能删除：${escapeHtml((item.delete_blockers || []).join("、") || "缺少 Python 安全许可")}</p>` : ""}
            ${mixedDetails}
            ${isLong ? `<button type='button' class='text-toggle quiet-button' data-review-text-toggle aria-controls='${textId}' aria-expanded='false'>展开完整正文</button>` : ""}
            <div class='request-fields'>
              <label>原因（先选择处理方式）<select data-review-reason ${decision ? "" : "disabled"}>${reasonOptions}</select></label>
              <label>备注（先选择处理方式，0–500 字）<textarea maxlength='500' rows='3' data-review-note placeholder='说明上下文、边界或判断依据' ${decision ? "" : "disabled"}>${escapeHtml(decision?.note || "")}</textarea></label>
              <p class='field-error' data-review-error role='status' aria-live='polite'>${escapeHtml(decisionError(item, decision))}</p>
              <button type='button' class='text-button' data-review-undo ${decision ? "" : "disabled"}>撤销本条</button>
            </div>
          </div>
        </div>
        <details class='technical-details' data-technical-id='${escapeHtml(item.candidate_id)}'${technicalOpen}><summary>技术详情与追踪号</summary>${technicalRows(item)}<div class='review-context'><p><b>前文</b>${escapeHtml(item.before)}</p><p><b>后文</b>${escapeHtml(item.after)}</p></div></details>
      </article>`;
    };

    let visibleItems = [];
    const patchSummary = () => {
      const count = Object.keys(state.decisions).length;
      selectedNode.textContent = count ? `已形成 ${count} 条复核请求` : "尚未形成复核请求";
      const checkedItems = state.checked_ids.map((candidateId) => itemById.get(candidateId)).filter(Boolean);
      const blocked = checkedItems.filter((item) => !item.batch_delete_allowed);
      const deleteButton = root.querySelector("[data-review-batch='delete']");
      deleteButton.disabled = !checkedItems.length || blocked.length > 0;
      batchStatus.textContent = !checkedItems.length
        ? "未勾选候选"
        : blocked.length
          ? `已勾选 ${checkedItems.length} 条；${blocked.length} 条无批量删除资格`
          : `已勾选 ${checkedItems.length} 条，均具备 Python 批量删除许可`;
    };

    const patchDecision = (candidateId) => {
      const item = itemById.get(candidateId);
      const decision = state.decisions[candidateId];
      const card = root.querySelector(`[data-active-candidate='${CSS.escape(candidateId)}']`);
      if (card) {
        card.querySelectorAll("[data-review-choice]").forEach((radio) => {
          radio.checked = decision?.verdict === radio.value;
        });
        const proposal = card.querySelector("[data-proposal-value]");
        if (proposal) proposal.textContent = decision ? VERDICT_LABELS[decision.verdict] : "未选择";
        const reason = card.querySelector("[data-review-reason]");
        if (reason) {
          reason.value = decision?.reason_code || "insufficient_context";
          reason.disabled = !decision;
        }
        const note = card.querySelector("[data-review-note]");
        if (note) {
          note.disabled = !decision;
          if (note.value !== (decision?.note || "")) note.value = decision?.note || "";
        }
        const error = card.querySelector("[data-review-error]");
        if (error) error.textContent = decisionError(item, decision);
        const undo = card.querySelector("[data-review-undo]");
        if (undo) undo.disabled = !decision;
      }
      const queue = root.querySelector(`[data-queue-candidate='${CSS.escape(candidateId)}'] [data-queue-decision]`);
      if (queue) queue.textContent = decision ? `请求${VERDICT_LABELS[decision.verdict]}` : item.formal_decision ? `正式${FORMAL_LABELS[item.formal_decision]}` : "待复核";
      patchSummary();
      saveState();
    };

    const captureOpenTechnical = () => {
      state.expanded_technical_ids = [...root.querySelectorAll("details[data-technical-id][open]")]
        .map((details) => details.dataset.technicalId).filter((candidateId) => itemById.has(candidateId));
    };

    const renderWorkbench = ({focusPager = false, restoreScroll = false} = {}) => {
      if (resultsNode.querySelector(".review-workbench")) captureOpenTechnical();
      const filtered = filteredItems();
      const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
      state.page = Math.max(1, Math.min(state.page, pageCount));
      visibleItems = filtered.slice((state.page - 1) * pageSize, state.page * pageSize);
      if (!visibleItems.some((item) => item.candidate_id === state.active_candidate_id)) {
        state.active_candidate_id = visibleItems.find((item) => item.needs_review)?.candidate_id || visibleItems[0]?.candidate_id || "";
      }
      const active = itemById.get(state.active_candidate_id);
      countNode.textContent = `符合条件 ${filtered.length} / 全部 ${items.length} 条，第 ${state.page} / ${pageCount} 页`;
      resultsNode.innerHTML = `<div class='review-workbench'>${renderActive(active)}<aside class='candidate-queue' aria-label='候选队列'>${renderQueue(visibleItems) || "<p class='empty-state'>没有符合条件的候选。</p>"}<nav class='pager' aria-label='候选分页'><button type='button' data-review-page='prev' ${state.page === 1 ? "disabled" : ""}>上一页</button><span aria-current='page'>${state.page} / ${pageCount}</span><button type='button' data-review-page='next' ${state.page === pageCount ? "disabled" : ""}>下一页</button></nav></aside></div>`;
      patchSummary();
      const restoredScroll = state.filters.scroll_y;
      saveState({preserveScroll: restoreScroll});
      if (focusPager) resultsNode.focus({preventScroll: true});
      if (restoreScroll) requestAnimationFrame(() => window.scrollTo({top: restoredScroll || 0, behavior: "instant"}));
    };

    const updateDecision = (candidateId, verdict, {note: suppliedNote = ""} = {}) => {
      const item = itemById.get(candidateId);
      if (!item || !VERDICTS.has(verdict)) return;
      if (verdict === "delete" && !item.delete_allowed && !item.segment_delete_allowed) return;
      const scrollLeft = window.scrollX;
      const scrollTop = window.scrollY;
      const previous = state.decisions[candidateId];
      state.decisions[candidateId] = {
        verdict,
        reason_code: previous?.reason_code || defaultReason(verdict),
        note: previous?.note || String(suppliedNote).trim().slice(0, 500),
      };
      patchDecision(candidateId);
      const restoreScroll = () => window.scrollTo({left: scrollLeft, top: scrollTop, behavior: "instant"});
      restoreScroll();
      requestAnimationFrame(() => requestAnimationFrame(restoreScroll));
    };

    root.addEventListener("input", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.matches("[data-review-search]")) {
        state.page = 1;
        renderWorkbench();
        target.focus({preventScroll: true});
        return;
      }
      const card = target.closest("[data-active-candidate]");
      const candidateId = card?.dataset.activeCandidate;
      if (!candidateId || !state.decisions[candidateId]) return;
      if (target.matches("[data-review-note]")) {
        state.decisions[candidateId].note = target.value.slice(0, 500);
        patchDecision(candidateId);
      }
    });

    root.addEventListener("keydown", (event) => {
      const target = event.target;
      if (
        !(target instanceof HTMLInputElement)
        || !target.matches("[data-review-choice]")
        || (event.key !== " " && event.key !== "Spacebar")
      ) return;
      // Browser defaults can scroll the document after changing a focused radio.
      // Apply the same native radio state explicitly so the review position is stable.
      event.preventDefault();
      if (target.disabled || target.checked) return;
      target.checked = true;
      target.dispatchEvent(new Event("change", {bubbles: true}));
    });

    root.addEventListener("change", async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.matches("[data-review-module],[data-review-scope],[data-review-status]")) {
        state.page = 1;
        renderWorkbench();
        target.focus({preventScroll: true});
        return;
      }
      if (target.matches("[data-review-choice]")) {
        updateDecision(target.dataset.candidateId, target.value);
        target.focus({preventScroll: true});
        return;
      }
      const card = target.closest("[data-active-candidate]");
      const candidateId = card?.dataset.activeCandidate;
      if (target.matches("[data-review-reason]") && candidateId && state.decisions[candidateId] && REASONS.has(target.value)) {
        state.decisions[candidateId].reason_code = target.value;
        patchDecision(candidateId);
        target.focus({preventScroll: true});
        return;
      }
      if (target.matches("[data-review-check]")) {
        const checkedId = target.dataset.reviewCheck;
        const checked = new Set(state.checked_ids);
        target.checked ? checked.add(checkedId) : checked.delete(checkedId);
        state.checked_ids = [...checked];
        patchSummary();
        saveState();
        return;
      }
      if (target.matches("[data-review-import-progress]")) {
        const file = target.files?.[0];
        if (!file || file.size > 1_000_000) {
          copyStatus.textContent = "进度文件缺失或过大，已拒绝导入。";
          return;
        }
        try {
          const imported = cleanState(JSON.parse(await file.text()));
          if (!imported) throw new Error("invalid state");
          state = imported;
          moduleInput.value = state.filters.module;
          scopeInput.value = state.filters.scope;
          statusInput.value = state.filters.status;
          searchInput.value = state.filters.search;
          renderWorkbench({restoreScroll: true});
          copyStatus.textContent = "进度已导入。";
        } catch {
          copyStatus.textContent = "进度与本页状态不匹配或内容无效，已拒绝导入。";
        } finally {
          target.value = "";
        }
      }
    });

    root.addEventListener("toggle", (event) => {
      if (event.target.matches?.("details[data-technical-id]")) {
        captureOpenTechnical();
        saveState();
      }
    }, true);

    root.addEventListener("click", async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const openButton = target.closest("[data-review-open]");
      if (openButton) {
        state.active_candidate_id = openButton.dataset.reviewOpen;
        renderWorkbench();
        const activeHeading = root.querySelector("[data-review-active-heading]");
        if (window.matchMedia("(max-width: 768px)").matches) {
          activeHeading?.focus?.({preventScroll: true});
          requestAnimationFrame(() => {
            activeHeading?.scrollIntoView?.({block: "start", behavior: "auto"});
          });
          return;
        }
        activeHeading?.focus?.({preventScroll: true});
        return;
      }
      if (target.matches("[data-review-text-toggle]")) {
        const original = document.getElementById(target.getAttribute("aria-controls") || "");
        const expanded = target.getAttribute("aria-expanded") === "true";
        original?.classList.toggle("is-expanded", !expanded);
        original?.classList.toggle("is-collapsed", expanded);
        target.setAttribute("aria-expanded", String(!expanded));
        target.textContent = expanded ? "展开完整正文" : "收起完整正文";
        return;
      }
      if (target.matches("[data-review-undo]")) {
        const candidateId = target.closest("[data-active-candidate]")?.dataset.activeCandidate;
        if (candidateId) {
          delete state.decisions[candidateId];
          patchDecision(candidateId);
        }
        return;
      }
      if (target.matches("[data-review-page]")) {
        state.page += target.dataset.reviewPage === "next" ? 1 : -1;
        renderWorkbench({focusPager: true});
        return;
      }
      if (target.matches("[data-review-check-visible]")) {
        const checked = new Set(state.checked_ids);
        visibleItems.forEach((item) => checked.add(item.candidate_id));
        state.checked_ids = [...checked];
        root.querySelectorAll("[data-review-check]").forEach((checkbox) => { checkbox.checked = true; });
        patchSummary();
        saveState();
        return;
      }
      if (target.matches("[data-check-group]")) {
        const checked = new Set(state.checked_ids);
        visibleItems.filter((item) => item.review_group_id === target.dataset.checkGroup).forEach((item) => checked.add(item.candidate_id));
        state.checked_ids = [...checked];
        root.querySelectorAll("[data-review-check]").forEach((checkbox) => { checkbox.checked = checked.has(checkbox.dataset.reviewCheck); });
        patchSummary();
        saveState();
        return;
      }
      if (target.matches("[data-review-batch]")) {
        const verdict = target.dataset.reviewBatch;
        const selected = state.checked_ids.map((candidateId) => itemById.get(candidateId)).filter(Boolean);
        if (!selected.length) return;
        const batchNote = String(batchNoteInput?.value || "").trim().slice(0, 500);
        if (verdict === "delete") {
          const blocked = selected.filter((item) => !item.batch_delete_allowed);
          if (blocked.length) {
            batchStatus.textContent = `${blocked.length} 条候选被 Python 安全规则阻止，未执行批量删除。`;
            return;
          }
          if (!window.confirm(`确认给 ${selected.length} 条候选形成删除请求？Agent 仍会重新校验并 dry-run。`)) return;
        }
        const noteRequired = selected.filter((item) => {
          const previous = decisionFor(item);
          const next = {
            verdict,
            reason_code: previous?.reason_code || defaultReason(verdict),
            note: previous?.note || batchNote,
          };
          return decisionError(item, next) === "此选择必须填写备注。";
        });
        if (noteRequired.length) {
          batchStatus.textContent = `${noteRequired.length} 条候选需要说明；请填写批量说明后再形成请求。`;
          batchNoteInput?.focus({preventScroll: true});
          return;
        }
        selected.forEach((item) => updateDecision(item.candidate_id, verdict, {note: batchNote}));
        patchDecision(state.active_candidate_id);
        return;
      }
      if (target.matches("[data-review-clear]")) {
        const requestCount = Object.keys(state.decisions).length;
        if (!requestCount) {
          copyStatus.textContent = "当前没有可清除的复核请求。";
          return;
        }
        if (!window.confirm(`确认清除 ${requestCount} 条尚未应用的复核请求？建议先导出进度 JSON。`)) return;
        state.decisions = {};
        patchDecision(state.active_candidate_id);
        root.querySelectorAll("[data-queue-decision]").forEach((node) => {
          const item = itemById.get(node.closest("[data-queue-candidate]")?.dataset.queueCandidate);
          node.textContent = item?.formal_decision ? `正式${FORMAL_LABELS[item.formal_decision]}` : "待复核";
        });
        copyStatus.textContent = "已清除全部尚未应用的复核请求。";
        return;
      }
      if (target.matches("[data-review-export-progress]")) {
        saveState();
        const blob = new Blob([JSON.stringify(state, null, 2)], {type: "application/json"});
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `review-progress-${payload.review_state_id.slice(0, 12)}.json`;
        link.click();
        URL.revokeObjectURL(link.href);
        copyStatus.textContent = "进度 JSON 已导出；文件不含正文。";
        return;
      }
      if (target.matches("[data-review-copy]")) {
        const requests = [];
        const errors = [];
        Object.entries(state.decisions).forEach(([candidateId, decision]) => {
          const item = itemById.get(candidateId);
          const error = decisionError(item, decision);
          if (error) errors.push(`候选 ${item.display_index}：${error}`);
          const requestItem = {
            candidate_id: candidateId,
            candidate_fingerprint: item.candidate_fingerprint,
            verdict: decision.verdict,
            reason_code: decision.reason_code,
            note: decision.note,
          };
          if (decision.verdict === "delete" && item.segment_delete_allowed) {
            requestItem.edit_plan_id = item.edit_plan_id;
            requestItem.splice_strategy = "exact_segment";
          }
          requests.push(requestItem);
        });
        if (!requests.length) {
          copyStatus.textContent = "请先为至少一条候选形成复核请求。";
          return;
        }
        if (errors.length) {
          copyStatus.textContent = errors[0];
          return;
        }
        const request = {
          schema: "cml.review-request.v1",
          workspace_identity: payload.workspace_identity,
          review_state_id: payload.review_state_id,
          requests,
        };
        try {
          await copyText(JSON.stringify(request, null, 2));
          copyStatus.textContent = "复核请求 JSON 已复制，请交给调用本 Skill 的 Agent。";
        } catch {
          copyStatus.textContent = "复制失败，请先导出进度 JSON 或手动复制。";
        }
      }
    });

    window.addEventListener("beforeunload", saveState);
    renderWorkbench({restoreScroll: true});
  });
})();
