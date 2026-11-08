frappe.pages["carts"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("carts"),
		single_column: true,
	});
	wrapper.bz_carts = new BzCartsPage(page);
	wrapper.bz_carts.load_route();
};

frappe.pages["carts"].on_page_show = function (wrapper) {
	const view = wrapper.bz_carts;
	if (view && frappe.get_route_str() !== view.current_route) {
		view.load_route();
	}
};

class BzCartsPage {
	constructor(page) {
		this.page = page;
		this.$body = page.body;
		this.current_route = "";
		this.status = "Active";
		this.q = "";
		this.page_no = 1;
		this.state = null;
		this.status_labels = {
			Active: __("نشطة"),
			Converted: __("محوّلة"),
			Expired: __("منتهية"),
		};
		this.action_labels = {
			View: __("عرض"),
			Copy: __("نسخ"),
			CSV: "CSV",
			Recheck: __("إعادة فحص"),
		};
	}

	load_route() {
		this.current_route = frappe.get_route_str();
		const route = frappe.get_route() || [];
		let name = "";
		if (route.length > 1) {
			try {
				name = decodeURIComponent(route[1]);
			} catch (e) {
				name = route[1];
			}
		}
		if (name) {
			this.load_detail(name);
		} else {
			this.load_list(1);
		}
	}

	async load_list(page_no) {
		this.state = null;
		this.page_no = page_no || 1;
		this.render_list_shell();
		this.$body.find("#bz-carts-list").html(
			`<div class="bz-empty">${__("جارٍ تحميل السلال...")}</div>`
		);

		try {
			const r = await frappe.call({
				method: "biozone_web.api.staff_cart_list",
				type: "POST",
				args: {
					status: this.status,
					q: this.q,
					page: this.page_no,
				},
				freeze: true,
				freeze_message: __("جارٍ تحميل السلال..."),
			});
			const data = r.message || {};
			if (!data.ok) {
				throw new Error(data.error || __("تعذر تحميل السلال"));
			}
			this.render_tabs(data.counts || {});
			this.render_list(data);
		} catch (error) {
			this.$body.find("#bz-carts-list").html(
				`<div class="bz-empty"><p>${this.esc(this.error_message(error))}</p>
				<button class="btn btn-default btn-sm" data-action="retry">${__("إعادة المحاولة")}</button></div>`
			);
		}
	}

	render_list_shell() {
		this.$body.html(`
			<div class="bz-carts-page" dir="rtl">
				<div class="bz-toolbar">
					<div class="bz-search">
						<input id="bz-carts-q" type="search" class="form-control input-sm"
							placeholder="${__("بحث بالعميل أو المستخدم أو رقم السلة...")}"
							value="${this.esc(this.q)}">
						<button class="btn btn-primary btn-sm" data-action="search">${__("بحث")}</button>
					</div>
					<div class="bz-tabs" id="bz-carts-tabs"></div>
				</div>
				<div class="text-muted small bz-summary" id="bz-carts-summary"></div>
				<div id="bz-carts-list"></div>
			</div>
		`);

		const $root = this.$body.find(".bz-carts-page");
		$root.on("click", (event) => {
			const retry = event.target.closest("[data-action='retry']");
			if (retry) {
				this.load_list(this.page_no);
				return;
			}
			const search = event.target.closest("[data-action='search']");
			if (search) {
				this.search();
				return;
			}
			const tab = event.target.closest("[data-status]");
			if (tab) {
				this.status = tab.dataset.status;
				this.load_list(1);
				return;
			}
			const open = event.target.closest("[data-open]");
			if (open) {
				frappe.set_route("carts", open.dataset.open);
				return;
			}
			const pager = event.target.closest("[data-page]");
			if (pager && !pager.disabled) {
				this.load_list(parseInt(pager.dataset.page, 10));
			}
		});
		$root.on("keydown", "#bz-carts-q", (event) => {
			if (event.key === "Enter") {
				event.preventDefault();
				this.search();
			}
		});
	}

	search() {
		this.q = (this.$body.find("#bz-carts-q").val() || "").trim();
		this.load_list(1);
	}

	render_tabs(counts) {
		const tabs = [
			["Active", __("نشطة")],
			["Converted", __("محوّلة")],
			["Expired", __("منتهية")],
			["All", __("الكل")],
		];
		const html = tabs
			.map(([key, label]) => {
				const count = key === "All" ? counts.All || 0 : counts[key] || 0;
				const active = this.status === key ? " active" : "";
				return `<button type="button" class="bz-tab${active}" data-status="${key}">
					${label}<span class="bz-tab-count">${count}</span>
				</button>`;
			})
			.join("");
		this.$body.find("#bz-carts-tabs").html(html);
	}

	render_list(data) {
		const carts = data.carts || [];
		const total = data.total || 0;
		const pages = data.pages || 1;
		const current = data.page || 1;
		this.$body
			.find("#bz-carts-summary")
			.text(`${total} ${__("سلة")} — ${__("صفحة")} ${current} ${__("من")} ${pages}`);

		if (!carts.length) {
			this.$body.find("#bz-carts-list").html(
				`<div class="bz-empty">
					<p class="font-weight-bold">${__("لا توجد سلال مطابقة")}</p>
					<p class="text-muted small">${__("جرّب تغيير الحالة أو كلمة البحث.")}</p>
				</div>`
			);
			return;
		}

		const rows = carts.map((row) => this.render_list_row(row)).join("");
		const pager = pages > 1 ? this.render_pager(current, pages) : "";
		this.$body.find("#bz-carts-list").html(`
			<div class="table-responsive">
				<table class="table table-bordered table-hover bz-table">
					<thead>
						<tr>
							<th>${__("السلة")}</th>
							<th>${__("العميل")}</th>
							<th>${__("الحالة")}</th>
							<th>${__("الأصناف")}</th>
							<th>${__("آخر نشاط")}</th>
							<th>${__("الانتهاء")}</th>
							<th></th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
			${pager}
		`);
	}

	render_list_row(row) {
		const name = this.esc(row.name);
		const customer = this.esc(row.customer_name || row.customer || "—");
		const email = this.esc(row.customer_email || row.user || "");
		const so = row.converted_sales_order
			? `<div class="small text-primary">SO: ${this.esc(row.converted_sales_order)}</div>`
			: "";
		const items = Number(row.item_count || 0);
		const units = Number(row.total_units || 0);
		return `<tr>
			<td><div class="bz-mono">${name}</div>${so}</td>
			<td>
				<div class="font-weight-bold">${customer}</div>
				<div class="bz-muted" dir="ltr">${email}</div>
			</td>
			<td>${this.status_badge(row.status)}</td>
			<td>
				<div class="font-weight-bold">${items}</div>
				<div class="bz-muted">${units} ${__("وحدة")}</div>
			</td>
			<td>${this.esc(this.format_dt(row.last_activity_on))}</td>
			<td>${this.esc(this.format_dt(row.expires_on))}</td>
			<td>
				<button type="button" class="btn btn-default btn-xs" data-open="${name}">
					${__("عرض")}
				</button>
			</td>
		</tr>`;
	}

	render_pager(current, pages) {
		return `<div class="bz-pager">
			<button type="button" class="btn btn-default btn-xs" data-page="${current - 1}"
				${current <= 1 ? "disabled" : ""}>${__("السابق")}</button>
			<span class="text-muted small">${__("صفحة")} ${current} ${__("من")} ${pages}</span>
			<button type="button" class="btn btn-default btn-xs" data-page="${current + 1}"
				${current >= pages ? "disabled" : ""}>${__("التالي")}</button>
		</div>`;
	}

	async load_detail(name) {
		this.state = null;
		this.$body.html(
			`<div class="bz-carts-page" dir="rtl"><div class="bz-empty">${__("جارٍ تحميل السلة...")}</div></div>`
		);

		try {
			const r = await frappe.call({
				method: "biozone_web.api.staff_cart_detail",
				type: "POST",
				args: { name: name },
				freeze: true,
				freeze_message: __("جارٍ تحميل السلة..."),
			});
			const data = r.message || {};
			if (!data.ok) {
				throw new Error(data.error || __("السلة غير موجودة"));
			}
			this.state = data;
			this.render_detail(data);
		} catch (error) {
			this.render_detail_error(this.error_message(error));
		}
	}

	render_detail_error(message) {
		this.$body.html(`
			<div class="bz-carts-page" dir="rtl">
				<div class="bz-empty">
					<p>${this.esc(message)}</p>
					<button type="button" class="btn btn-default btn-sm" data-action="back">
						${__("العودة إلى قائمة السلال")}
					</button>
				</div>
			</div>
		`);
		this.$body.find("[data-action='back']").on("click", () => frappe.set_route("carts"));
	}

	render_detail(state) {
		const cart = state.cart || {};
		const quote = state.quote || {};
		const customer = state.customer || {};
		const access_log = state.access_log || [];
		const items = quote.items || [];
		const item_rows = items.length
			? items.map((item) => this.render_item_row(item)).join("")
			: `<tr><td colspan="5" class="text-center text-muted">${__("لا توجد أصناف في هذه السلة.")}</td></tr>`;
		const access_rows = access_log.length
			? access_log.map((row) => this.render_access_row(row)).join("")
			: `<tr><td colspan="4" class="text-center text-muted">${__("لا يوجد سجل بعد.")}</td></tr>`;
		const alert = quote.valid === false
			? `<div class="bz-alert">${__("هذه السلة تحتوي أصنافًا غير متاحة أو مشكلة سعرية. استخدم «إعادة الفحص» لعرض الفروق.")}</div>`
			: "";
		const so = cart.converted_sales_order
			? `<div class="small text-primary font-weight-bold">SO: ${this.esc(cart.converted_sales_order)}</div>`
			: "";

		this.$body.html(`
			<div class="bz-carts-page" dir="rtl">
				<div class="bz-detail-head">
					<div>
						<div class="text-muted small">
							<a href="#" data-action="back">${__("carts")}</a>
							<span>/</span>
							<span class="bz-mono">${this.esc(cart.name)}</span>
						</div>
						<h3>${__("تفاصيل السلة")}</h3>
					</div>
					<div class="bz-head-actions">
						${this.status_badge(cart.status)}
						<button type="button" class="btn btn-default btn-sm" data-action="back">
							${__("رجوع")}
						</button>
					</div>
				</div>

				<div class="bz-cards">
					<div class="bz-card">
						<div class="bz-card-label">${__("العميل")}</div>
						<div class="bz-card-value">${this.esc(customer.customer_name || customer.name || "—")}</div>
						<div class="bz-muted" dir="ltr">${this.esc(customer.email || cart.customer || "—")}</div>
						<div class="bz-muted">${this.esc(customer.customer_group || "")}</div>
					</div>
					<div class="bz-card">
						<div class="bz-card-label">${__("آخر نشاط")}</div>
						<div class="bz-card-value">${this.esc(this.format_dt(cart.last_activity_on))}</div>
						<div class="bz-muted">${__("الإصدار")} ${this.esc(cart.version || 0)}</div>
					</div>
					<div class="bz-card">
						<div class="bz-card-label">${__("تنتهي في")}</div>
						<div class="bz-card-value">${this.esc(this.format_dt(cart.expires_on))}</div>
						<div class="bz-muted">${__("30 يومًا من آخر نشاط")}</div>
					</div>
					<div class="bz-card">
						<div class="bz-card-label">${__("الإجمالي الحالي")}</div>
						<div class="bz-card-value">${this.fmt_money(quote.net_total, quote.currency)}</div>
						<div class="bz-muted">${this.esc(quote.item_count || 0)} ${__("صنف")} — ${this.esc(quote.total_units || 0)} ${__("وحدة")}</div>
						${so}
					</div>
				</div>

				${alert}

				<div class="bz-section">
					<div class="bz-section-head">
						<div>
							<div class="bz-section-title">${__("الأصناف")}</div>
							<div class="bz-muted">${__("عرض للقراءة فقط — لا يمكن تعديل السلة من هنا.")}</div>
						</div>
						<div class="bz-actions">
							<label><input type="checkbox" id="bz-include-customer"> ${__("تضمين بيانات العميل")}</label>
							<label><input type="checkbox" id="bz-include-prices"> ${__("تضمين الأسعار")}</label>
							<button type="button" class="btn btn-primary btn-sm" data-action="recheck">
								${__("إعادة الفحص")}
							</button>
							<button type="button" class="btn btn-default btn-sm" data-action="copy">
								${__("نسخ")}
							</button>
							<button type="button" class="btn btn-default btn-sm" data-action="csv">
								CSV
							</button>
						</div>
					</div>
					<div class="table-responsive">
						<table class="table table-bordered bz-table">
							<thead>
								<tr>
									<th>${__("الصنف")}</th>
									<th>${__("الكمية")}</th>
									<th>${__("السعر")}</th>
									<th>${__("الإجمالي")}</th>
									<th>${__("الحالة")}</th>
								</tr>
							</thead>
							<tbody>${item_rows}</tbody>
						</table>
					</div>
				</div>

				<div class="bz-section" id="bz-recheck-panel" hidden>
					<div class="bz-section-head">
						<div class="bz-section-title">${__("نتيجة إعادة الفحص")}</div>
						<div class="text-muted small" id="bz-recheck-time"></div>
					</div>
					<div class="bz-panel-body" id="bz-recheck-results"></div>
				</div>

				<div class="bz-section">
					<div class="bz-section-head">
						<div>
							<div class="bz-section-title">${__("سجل الوصول")}</div>
							<div class="bz-muted">${__("آخر 20 عملية على هذه السلة — يُحذف السجل بعد 7 أيام.")}</div>
						</div>
					</div>
					<div class="table-responsive">
						<table class="table table-bordered bz-table">
							<thead>
								<tr>
									<th>${__("العملية")}</th>
									<th>${__("المستخدم")}</th>
									<th>${__("الوقت")}</th>
									<th>${__("تفاصيل")}</th>
								</tr>
							</thead>
							<tbody>${access_rows}</tbody>
						</table>
					</div>
				</div>
			</div>
		`);

		this.bind_detail_actions();
	}

	render_item_row(item) {
		const issues = (item.issues || [])
			.map((issue) => issue.message || issue.code || "")
			.filter(Boolean)
			.join(" — ");
		const available = item.available
			? `<span class="bz-status bz-status-active">${__("متاح")}</span>`
			: `<span class="bz-status bz-status-expired">${__("غير متاح")}</span>`;
		return `<tr>
			<td>
				<div class="font-weight-bold">${this.esc(item.item_name || item.item_code)}</div>
				<div class="bz-mono">${this.esc(item.item_code)}</div>
				${issues ? `<div class="small text-danger font-weight-bold">${this.esc(issues)}</div>` : ""}
			</td>
			<td class="font-weight-bold">${this.esc(item.qty)} <span class="bz-muted">${this.esc(item.uom || "")}</span></td>
			<td>${item.price === null || item.price === undefined ? "—" : this.esc(Number(item.price).toFixed(2))}</td>
			<td class="font-weight-bold">${item.line_total === null || item.line_total === undefined ? "—" : this.esc(Number(item.line_total).toFixed(2))}</td>
			<td>${available}</td>
		</tr>`;
	}

	render_access_row(row) {
		return `<tr>
			<td class="font-weight-bold">${this.esc(this.action_labels[row.action] || row.action)}</td>
			<td class="bz-mono">${this.esc(row.staff_user)}</td>
			<td class="bz-muted">${this.esc(this.format_dt(row.creation))}</td>
			<td class="bz-muted">${this.esc(row.details || "")}</td>
		</tr>`;
	}

	bind_detail_actions() {
		this.$body.find("[data-action='back']").on("click", (event) => {
			event.preventDefault();
			frappe.set_route("carts");
		});
		this.$body.find("[data-action='recheck']").on("click", (event) => {
			this.do_recheck(event.currentTarget);
		});
		this.$body.find("[data-action='copy']").on("click", (event) => {
			this.do_copy(event.currentTarget);
		});
		this.$body.find("[data-action='csv']").on("click", (event) => {
			this.do_csv(event.currentTarget);
		});
	}

	async do_recheck(button) {
		if (!this.state) return;
		this.set_busy(button, true);
		try {
			const data = await this.call("staff_cart_recheck", {
				name: this.state.cart.name,
			});
			this.render_recheck(data);
		} catch (error) {
			frappe.msgprint({
				title: __("تعذر إعادة الفحص"),
				message: this.error_message(error),
				indicator: "red",
			});
		} finally {
			this.set_busy(button, false);
		}
	}

	async do_copy(button) {
		if (!this.state) return;
		this.set_busy(button, true);
		try {
			const data = await this.call("staff_cart_copy", this.copy_options());
			await frappe.utils.copy_to_clipboard(data.text || "");
			frappe.show_alert({
				message: __("تم نسخ السلة إلى الحافظة."),
				indicator: "green",
			});
		} catch (error) {
			frappe.msgprint({
				title: __("تعذر النسخ"),
				message: this.error_message(error),
				indicator: "red",
			});
		} finally {
			this.set_busy(button, false);
		}
	}

	async do_csv(button) {
		if (!this.state) return;
		this.set_busy(button, true);
		try {
			const data = await this.call("staff_cart_csv", this.copy_options());
			const blob = new Blob([data.csv || ""], { type: "text/csv;charset=utf-8;" });
			const url = URL.createObjectURL(blob);
			const link = document.createElement("a");
			link.href = url;
			link.download = data.filename || `cart-${this.state.cart.name}.csv`;
			document.body.appendChild(link);
			link.click();
			link.remove();
			setTimeout(() => URL.revokeObjectURL(url), 1000);
			frappe.show_alert({
				message: __("تم تنزيل ملف CSV."),
				indicator: "green",
			});
		} catch (error) {
			frappe.msgprint({
				title: __("تعذر تنزيل CSV"),
				message: this.error_message(error),
				indicator: "red",
			});
		} finally {
			this.set_busy(button, false);
		}
	}

	copy_options() {
		return {
			name: this.state.cart.name,
			include_customer: this.$body.find("#bz-include-customer").is(":checked") ? 1 : 0,
			include_prices: this.$body.find("#bz-include-prices").is(":checked") ? 1 : 0,
		};
	}

	render_recheck(data) {
		const panel = this.$body.find("#bz-recheck-panel");
		panel.prop("hidden", false);
		panel.find("#bz-recheck-time").text(`${__("آخر فحص")}: ${this.format_dt(data.checked_on)}`);
		const results = panel.find("#bz-recheck-results").empty();
		const changes = this.compare_quotes(this.state.quote || {}, data.quote || {});
		if (!changes.length) {
			results.append(
				`<p class="text-success font-weight-bold">${__("لا توجد تغييرات — الأسعار والتوفر مطابقان لآخر تحميل.")}</p>`
			);
			return;
		}
		changes.forEach((change) => {
			results.append(`
				<div class="bz-change">
					<span class="bz-change-name">${this.esc(change.name)}</span>
					<span class="bz-change-text">${this.esc(change.text)}</span>
				</div>
			`);
		});
	}

	compare_quotes(before, after) {
		const old = {};
		(before.items || []).forEach((item) => {
			old[this.item_key(item)] = item;
		});
		const changes = [];
		(after.items || []).forEach((item) => {
			const prev = old[this.item_key(item)];
			if (!prev) {
				changes.push({
					name: item.item_name || item.item_code,
					text: __("صنف جديد في التسعير الحالي"),
				});
				return;
			}
			if (Number(prev.price) !== Number(item.price)) {
				const before_price = prev.price == null ? "—" : Number(prev.price).toFixed(2);
				const after_price = item.price == null ? "—" : Number(item.price).toFixed(2);
				changes.push({
					name: item.item_name || item.item_code,
					text: `${__("تغيّر السعر")}: ${before_price} ← ${after_price}`,
				});
			}
			if (Boolean(prev.available) !== Boolean(item.available)) {
				changes.push({
					name: item.item_name || item.item_code,
					text: item.available ? __("أصبح متاحًا") : __("لم يعد متاحًا"),
				});
			}
			const before_issues = (prev.issues || []).map((x) => x.code || x.message || "").join("|");
			const after_issues = (item.issues || []).map((x) => x.code || x.message || "").join("|");
			if (before_issues !== after_issues) {
				changes.push({
					name: item.item_name || item.item_code,
					text: __("تغيّرت مشكلات التوفر/الوحدة"),
				});
			}
		});
		if (Number(before.net_total) !== Number(after.net_total)) {
			changes.push({
				name: __("الإجمالي"),
				text: `${__("تغيّر الإجمالي")}: ${Number(before.net_total || 0).toFixed(2)} ← ${Number(after.net_total || 0).toFixed(2)} ${after.currency || ""}`,
			});
		}
		return changes;
	}

	item_key(item) {
		return `${item.item_code || ""}|${item.uom || ""}`;
	}

	async call(method, args) {
		const r = await frappe.call({
			method: `biozone_web.api.${method}`,
			type: "POST",
			args: args || {},
			freeze: true,
		});
		const data = r.message || {};
		if (!data.ok) {
			throw new Error(data.error || __("تعذر تنفيذ العملية"));
		}
		return data;
	}

	set_busy(button, busy) {
		if (!button) return;
		button.disabled = busy;
	}

	status_badge(status) {
		const key = String(status || "").toLowerCase();
		const label = this.status_labels[status] || status || "—";
		return `<span class="bz-status bz-status-${this.esc(key)}">${this.esc(label)}</span>`;
	}

	format_dt(value) {
		if (!value) return "—";
		try {
			return frappe.datetime.str_to_user(value);
		} catch (e) {
			return String(value);
		}
	}

	fmt_money(value, currency) {
		if (value === null || value === undefined || value === "") return "—";
		const number = Number(value);
		if (Number.isNaN(number)) return this.esc(value);
		const suffix = currency ? ` ${this.esc(currency)}` : "";
		return `${number.toFixed(2)}${suffix}`;
	}

	esc(value) {
		return frappe.utils.escape_html(value === null || value === undefined ? "" : String(value));
	}

	error_message(error) {
		if (error && error.message) return error.message;
		if (error && error._server_messages) {
			try {
				const messages = JSON.parse(error._server_messages);
				return messages
					.map((message) => {
						try {
							return JSON.parse(message).message;
						} catch (e) {
							return message;
						}
					})
					.join(" ");
			} catch (e) {
				return error._server_messages;
			}
		}
		return __("تعذر تنفيذ العملية. حدّث الصفحة وحاول مرة أخرى.");
	}
}
