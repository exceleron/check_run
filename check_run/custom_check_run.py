import frappe, json
from frappe.utils.data import  flt, get_datetime, getdate, nowdate
from check_run.check_run.doctype.check_run.check_run import CheckRun, has_permission, calculate_payment_term_discount, get_attachments
from frappe.query_builder.custom import ConstantColumn
from frappe.query_builder.functions import Coalesce, NullIf
import datetime
from check_run.check_run.doctype.check_run.check_run import get_check_run_settings
@frappe.whitelist()
def process_check_run(docname: str) -> None:
	has_permission("Check Run", ptype="submit", user=frappe.session.user, raise_exception=True)
	doc = frappe.get_doc("Check Run", docname)
	custom_process_check_run(doc)

def custom_process_check_run(self):
	check_run_submitting = frappe.defaults.get_global_default("check_run_submitting")
	if check_run_submitting:
		frappe.throw(
			frappe._(
				f"""Check run {check_run_submitting} is in process. No other check runs can be submitted until it completes. <a href="/app/background_jobs">Click here</a> for details."""
			)
		)
		return
	CheckRun.validate(self)
	self.status = "Submitting"
	CheckRun.filter_transactions(self)
	transactions = [
		t
		for t in json.loads(self.transactions)
		if t.get("pay") and not self.not_outstanding_or_cancelled(t)
	]
	if not len(transactions):
		frappe.msgprint(
			"Please check; maybe you have not selected any document to pay, or the selected document has already been paid.<br><br>Kindly refresh the page."
		)
		return

	self.print_count = 0
	if self.ach_only().ach_only:
		self.initial_check_number = ""  # type: ignore
		self.final_check_number = ""
	settings = get_check_run_settings(self)
	if settings.include_payment_validation == 1:
		frappe.enqueue(process_check_runn,
				self=self,
				queue="short",
				timeout=3600,
				now=True)

def process_check_runn(self, save: bool = False) -> None:
	frappe.defaults.set_global_default("check_run_submitting", self.name)
	frappe.db.sql("SAVEPOINT process_check_run")
	try:
		__transactions = self.transactions
		# Parse transactions JSON
		_transactions = json.loads(__transactions)

		# Filter only payable transactions
		payable_transactions = [
			frappe._dict(txn)
			for txn in _transactions
			if txn.get("pay")
		]

		# Calculate total payable amount
		total_amount = sum(flt(txn.amount) for txn in payable_transactions)

		# Get bank balance (example – adjust as per your logic)
		bank_balance = flt(self.beg_balance)  # or fetch from Bank Account

		# Validate against bank balance
		if total_amount > bank_balance:
			frappe.throw(
				f"Total payment amount ({total_amount}) exceeds "
				f"available bank balance ({bank_balance})."
			)

		# Sort and proceed
		transactions = sorted(payable_transactions, key=lambda x: x.party)

		_transactions = self.create_payment_entries(transactions)
	except Exception as e:
		try:
			frappe.db.rollback(save_point="process_check_run")
		except Exception as _e:
			pass
		frappe.defaults.clear_default("check_run_submitting")
		raise e

	frappe.defaults.clear_default("check_run_submitting")
	self.transactions = json.dumps(_transactions)
	self.set_status("Submitted")
	self.save()
	self.submit()
	self.create_and_attach_positive_pay()
	frappe.db.sql("RELEASE SAVEPOINT process_check_run")


@frappe.whitelist()
@frappe.read_only()
def process_get_entries(doc: CheckRun | str) -> dict:
	doc = frappe._dict(json.loads(doc)) if isinstance(doc, str) else doc  # type: ignore
	if isinstance(doc.end_date, str):
		doc.end_date = getdate(doc.end_date)  # type: ignore
	doc.posting_date = getdate(doc.posting_date)  # type: ignore
	modes_of_payment = [""] + frappe.get_all("Mode of Payment", order_by="name", pluck="name")

	# Fetch Check Run Settings if available
	if frappe.db.exists(
		"Check Run Settings", {"bank_account": doc.bank_account, "pay_to_account": doc.pay_to_account}
	):
		settings = frappe.get_doc(
			"Check Run Settings", {"bank_account": doc.bank_account, "pay_to_account": doc.pay_to_account}
		)
		settings.include_expense_claims = 0  #hrms-removal 2026
	else:
		settings = None

	db_doc = None
	if frappe.db.exists("Check Run", doc.name):
		db_doc = frappe.get_doc("Check Run", doc.name)
		if not doc.modified or get_datetime(doc.modified) < db_doc.modified:
			doc = db_doc
		if doc.end_date == db_doc.end_date and db_doc.transactions:  # type: ignore
			if db_doc.docstatus == 0:
				outstanding_transaction = []
				for row in json.loads(db_doc.transactions):
					if not db_doc.not_outstanding_or_cancelled(row):
						outstanding_transaction.append(row)
			else:
				outstanding_transaction = json.loads(db_doc.transactions)
			return {"transactions": outstanding_transaction, "modes_of_payment": modes_of_payment}

	company = doc.company  # type: ignore
	pay_to_account = doc.pay_to_account  # type: ignore
	end_date = doc.end_date  # type: ignore

	# Build Purchase Invoices Query
	payment_schedule = frappe.qb.DocType("Payment Schedule")
	purchase_invoices = frappe.qb.DocType("Purchase Invoice")
	suppliers = frappe.qb.DocType("Supplier")
	supplier_mop_sub_query = (
		frappe.qb.from_(suppliers)
		.select(suppliers.supplier_default_mode_of_payment)
		.as_("supplier_default_mode_of_payment")
		.where(purchase_invoices.supplier == suppliers.name)
	)
	stand_alone_debit_note_filter = (
		(Coalesce(payment_schedule.outstanding, purchase_invoices.outstanding_amount) > 0)
		if settings.allow_stand_alone_debit_notes == "No"
		else (Coalesce(payment_schedule.outstanding, purchase_invoices.outstanding_amount) != 0)
	)

	# Create Purchase Invoice Query with the filter to exclude paid invoices
	pi_qb = (
		frappe.qb.from_(purchase_invoices)
		.left_join(payment_schedule)
		.on(purchase_invoices.name == payment_schedule.parent)
		.select(
			ConstantColumn("Purchase Invoice").as_("doctype"),
			ConstantColumn("Supplier").as_("party_type"),
			purchase_invoices.name,
			(purchase_invoices.bill_no).as_("ref_number"),
			(purchase_invoices.supplier).as_("party"),
			(purchase_invoices.supplier_name).as_("party_name"),
			Coalesce(payment_schedule.outstanding, purchase_invoices.outstanding_amount).as_("amount"),
			Coalesce(payment_schedule.due_date, purchase_invoices.due_date).as_("due_date"),
			purchase_invoices.posting_date,
			Coalesce(
				purchase_invoices.supplier_default_mode_of_payment,
				NullIf(supplier_mop_sub_query, ""),
				f"{settings.purchase_invoice}" or "\n",
			).as_("mode_of_payment"),
			(payment_schedule.payment_term).as_("payment_term"),
		)
		.where(Coalesce(payment_schedule.due_date, purchase_invoices.due_date) <= end_date)
		.where(Coalesce(payment_schedule.outstanding, purchase_invoices.outstanding_amount) != 0)
		.where(stand_alone_debit_note_filter)
		.where(purchase_invoices.company == company)
		.where(purchase_invoices.docstatus == 1)
		.where(purchase_invoices.credit_to == pay_to_account)
		.where(purchase_invoices.status != "Debit Note Issued")
		.where(Coalesce(purchase_invoices.release_date, datetime.date(1900, 1, 1)) < end_date)
		# Exclude invoices that have already been paid (cleared via Payment Entry or Journal Entry)
		.where(
			purchase_invoices.name.notin(
				frappe.qb.from_("Payment Entry Reference")
				.inner_join(frappe.qb.DocType("Payment Entry"))
				.on(frappe.qb.DocType("Payment Entry").name == frappe.qb.DocType("Payment Entry Reference").parent)
				.select(frappe.qb.DocType("Payment Entry Reference").reference_name)
				.where(frappe.qb.DocType("Payment Entry Reference").reference_doctype == "Purchase Invoice")
				.where(frappe.qb.DocType("Payment Entry").docstatus == 1)
				.where(frappe.qb.DocType("Payment Entry").party == purchase_invoices.supplier)
			)
		)
	)

	# Build Expense Claims Query
	exp_claims = frappe.qb.DocType("Expense Claim")
	employees = frappe.qb.DocType("Employee")
	ec_qb = (
		frappe.qb.from_(exp_claims)
		.inner_join(employees)
		.on(exp_claims.employee == employees.name)
		.select(
			ConstantColumn("Expense Claim").as_("doctype"),
			ConstantColumn("Employee").as_("party_type"),
			exp_claims.name,
			(exp_claims.name).as_("ref_number"),
			(exp_claims.employee).as_("party"),
			(employees.employee_name).as_("party_name"),
			(exp_claims.grand_total).as_("amount"),
			(exp_claims.posting_date).as_("due_date"),
			exp_claims.posting_date,
			Coalesce(
				exp_claims.mode_of_payment,
				NullIf(employees.mode_of_payment, ""),
				f"{settings.expense_claim}" or "\n",
			).as_("mode_of_payment"),
			ConstantColumn("").as_("payment_term"),
		)
		.where(exp_claims.grand_total > exp_claims.total_amount_reimbursed)
		.where(exp_claims.company == company)
		.where(exp_claims.docstatus == 1)
		.where(exp_claims.payable_account == pay_to_account)
		.where(exp_claims.posting_date <= end_date)
	)

	# Build Journal Entries Query
	journal_entries = frappe.qb.DocType("Journal Entry")
	je_accounts = frappe.qb.DocType("Journal Entry Account")
	payment_entries = frappe.qb.DocType("Payment Entry")
	pe_ref = frappe.qb.DocType("Payment Entry Reference")

	sub_q = (
		frappe.qb.from_(payment_entries)
		.inner_join(pe_ref)
		.on(payment_entries.name == pe_ref.parent)
		.select(pe_ref.reference_name)
		.where(pe_ref.reference_doctype == "Journal Entry")
		.where(payment_entries.party == je_accounts.party)
		.where(payment_entries.docstatus == 1)
	)

	je_qb = (
		frappe.qb.from_(journal_entries)
		.inner_join(je_accounts)
		.on(journal_entries.name == je_accounts.parent)
		.select(
			ConstantColumn("Journal Entry").as_("doctype"),
			je_accounts.party_type,
			je_accounts.name,
			(journal_entries.name).as_("ref_number"),
			je_accounts.party,
			(je_accounts.party).as_("party_name"),
			(je_accounts.credit_in_account_currency).as_("amount"),
			journal_entries.due_date,
			journal_entries.posting_date,
			Coalesce(journal_entries.mode_of_payment, f"{settings.journal_entry}" or "\n").as_(
				"mode_of_payment"
			),
			ConstantColumn("").as_("payment_term"),
		)
		.where(journal_entries.company == company)
		.where(journal_entries.docstatus == 1)
		.where(je_accounts.account == pay_to_account)
		.where(journal_entries.due_date <= end_date)
		.where((journal_entries.name).notin(sub_q))  # codespell:ignore
	)

	if not settings:
		query = pi_qb.union(ec_qb).union(je_qb)
	else:
		query = ""
		flags = (
			settings.include_purchase_invoices,
			settings.include_expense_claims,
			settings.include_journal_entries,
		)
		for flag, qb in zip(flags, (pi_qb, ec_qb, je_qb)):
			if not flag:
				continue
			if not query:
				query = qb
			else:
				query = query.union(qb)  # type: ignore
	if query:
		query = query.orderby("due_date", "name").get_sql()

	transactions = frappe.db.sql(
		query,
		{"company": company, "pay_to_account": pay_to_account, "end_date": end_date},
		as_dict=True
	)
	file_preview_allowed = False if len(transactions) > settings.file_preview_threshold else True

	posting_date = getattr(doc, "posting_date", None)
	for transaction in transactions:
		if transaction.doctype == "Purchase Invoice" and transaction.payment_term and posting_date:
			discount_amount, has_discount = calculate_payment_term_discount(transaction, posting_date)
			transaction.discount_amount = transaction.amount - discount_amount if has_discount else 0.0
			transaction.has_discount = has_discount
		else:
			transaction.has_discount = False

		if file_preview_allowed:
			doc_name = transaction.ref_number if transaction.ref_number else transaction.name
			transaction.attachments = [
				attachment
				for attachment in get_attachments(transaction.doctype, doc_name)
				if attachment.file_url.endswith(".pdf")
			] or [
				{"file_name": doc_name, "file_url": f"/app/Form/{transaction.doctype}/{doc_name}"}
			]

		if settings and settings.pre_check_overdue_items:
			if transaction.due_date < doc.posting_date and not transaction.get("on_hold"):  # type: ignore
				transaction.pay = 1
		if transaction.doctype == "Journal Entry":
			if transaction.party_type == "Supplier":
				transaction.party_name = frappe.get_value("Supplier", transaction.party, "supplier_name")
				transaction.mode_of_payment = (
					frappe.get_value("Supplier", transaction.party, "supplier_default_mode_of_payment")
					or settings.journal_entry
				)
			if transaction.party_type == "Employee":
				transaction.party_name = frappe.get_value("Employee", transaction.party, "employee_name")
				transaction.mode_of_payment = (
					frappe.get_value("Employee", transaction.party, "mode_of_payment") or settings.journal_entry
				)

		if transaction.due_date and settings.show_due_date == "Show Days Past Due":
			transaction.due_date = (getdate(nowdate()) - transaction.due_date).days

	has_discounts = any(t.get("has_discount") for t in transactions)
	if has_discounts and settings and not settings.payment_discount_account:
		frappe.throw(
			frappe._(
				"Payment Discount Account is required in Check Run Settings when processing invoices with payment term discounts. Please configure the Payment Discount Account in Check Run Settings."
			)
		)

	outstanding_transaction = []
	if not isinstance(doc, CheckRun):
		if db_doc:
			doc = db_doc
		else:
			doc = frappe.get_doc("Check Run")
	for row in transactions:
		if not doc.not_outstanding_or_cancelled(row):  # type: ignore
			outstanding_transaction.append(row)
	# end
	return {"transactions": outstanding_transaction, "modes_of_payment": modes_of_payment}