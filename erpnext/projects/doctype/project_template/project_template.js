// Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Project Template', {
	// refresh: function(frm) {

	// }
});
frappe.ui.form.on('Project Template Task', {
	parent_task:function(frm,cdt,cdn){
		var row = locals[cdt][cdn];
		if(row.parent_task != null && (row.parent_task <= 0 || row.parent_task > frm.doc.tasks.length || row.parent_task === row.idx)){
			frappe.msgprint(__('Invalid Parent Task value'));
			frappe.model.set_value(cdt,cdn,"parent_task",null);
		}
	}
});