# -*- coding: utf-8 -*-
# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import datetime
import json

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.party import get_due_date
from frappe import _
from frappe.contacts.doctype.address.address import get_address_display
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, get_link_to_form, nowdate, today, unique


class DeliveryTrip(Document):
	def __init__(self, *args, **kwargs):
		super(DeliveryTrip, self).__init__(*args, **kwargs)

		# Google Maps returns distances in meters by default
		self.default_distance_uom = frappe.db.get_single_value("Global Defaults", "default_distance_unit") or "Meter"
		self.uom_conversion_factor = frappe.db.get_value("UOM Conversion Factor",
			{"from_uom": "Meter", "to_uom": self.default_distance_uom}, "value")

	def validate(self):
		self.validate_stop_addresses()
		self.update_status()
		self.update_package_total()

	def on_submit(self):
		self.update_status()
		self.update_delivery_notes()

	def on_update_after_submit(self):
		self.update_status()

	def on_cancel(self):
		self.update_status()
		self.update_delivery_notes(delete=True)

	def update_package_total(self):
		self.package_total = sum([stop.grand_total for stop in self.delivery_stops if stop.grand_total])

	def validate_stop_addresses(self):
		for stop in self.delivery_stops:
			if not stop.customer_address:
				stop.customer_address = get_address_display(frappe.get_doc("Address", stop.address).as_dict())

	def update_status(self):
		status = {
			0: "Draft",
			1: "Scheduled",
			2: "Cancelled"
		}[self.docstatus]

		if self.docstatus == 1:
			visited_stops = [stop.visited for stop in self.delivery_stops]
			if all(visited_stops):
				status = "Completed"
			elif any(visited_stops):
				status = "In Transit"

		self.db_set("status", status)

	def update_delivery_notes(self, delete=False):
		"""
		Update all connected Delivery Notes with Delivery Trip details
		(Driver, Vehicle, etc.). If `delete` is `True`, then details
		are removed.

		Args:
			delete (bool, optional): Defaults to `False`. `True` if driver details need to be emptied, else `False`.
		"""

		delivery_notes = list(set([stop.delivery_note for stop in self.delivery_stops if stop.delivery_note]))

		update_fields = {
			"driver": self.driver,
			"driver_name": self.driver_name,
			"vehicle_no": self.vehicle,
			"lr_no": self.name,
			"lr_date": self.departure_time
		}

		for delivery_note in delivery_notes:
			note_doc = frappe.get_doc("Delivery Note", delivery_note)

			for field, value in update_fields.items():
				value = None if delete else value
				setattr(note_doc, field, value)

			if delete:
				setattr(note_doc, "delivered", 0)
				setattr(note_doc, "status", "To Deliver")

			note_doc.flags.ignore_validate_update_after_submit = True
			note_doc.save()

		delivery_notes = [get_link_to_form("Delivery Note", note) for note in delivery_notes]
		frappe.msgprint(_("Delivery Notes {0} updated".format(", ".join(delivery_notes))))

	def process_route(self, optimize):
		"""
		Estimate the arrival times for each stop in the Delivery Trip.
		If `optimize` is True, the stops will be re-arranged, based
		on the optimized order, before estimating the arrival times.

		Args:
			optimize (bool): True if route needs to be optimized, else False
		"""

		departure_datetime = get_datetime(self.departure_time)
		route_list = self.form_route_list(optimize)

		# For locks, maintain idx count while looping through route list
		idx = 0
		for route in route_list:
			directions = self.get_directions(route, optimize)

			if directions:
				if optimize and len(directions.get("waypoint_order")) > 1:
					self.rearrange_stops(directions.get("waypoint_order"), start=idx)

				# Avoid estimating last leg back to the home address
				legs = directions.get("legs")[:-1] if route == route_list[-1] else directions.get("legs")

				# Google Maps returns the legs in the optimized order
				for leg in legs:
					delivery_stop = self.delivery_stops[idx]

					delivery_stop.lat, delivery_stop.lng = leg.get("end_location", {}).values()
					delivery_stop.uom = self.default_distance_uom
					distance = leg.get("distance", {}).get("value", 0.0)  # in meters
					delivery_stop.distance = distance * self.uom_conversion_factor

					duration = leg.get("duration", {}).get("value", 0)
					estimated_arrival = departure_datetime + datetime.timedelta(seconds=duration)
					delivery_stop.estimated_arrival = estimated_arrival

					stop_delay = frappe.db.get_single_value("Delivery Settings", "stop_delay")
					departure_datetime = estimated_arrival + datetime.timedelta(minutes=cint(stop_delay))
					idx += 1

				# Include last leg in the final distance calculation
				self.uom = self.default_distance_uom
				total_distance = sum([leg.get("distance", {}).get("value", 0.0)
					for leg in directions.get("legs")])  # in meters
				self.total_distance = total_distance * self.uom_conversion_factor
			else:
				idx += len(route) - 1

		self.save()

	def form_route_list(self, optimize):
		"""
		Form a list of address routes based on the delivery stops. If locks
		are present, and the routes need to be optimized, then they will be
		split into sublists at the specified lock position(s).

		Args:
			optimize (bool): `True` if route needs to be optimized, else `False`

		Returns:
			(list of list of str): List of address routes split at locks, if optimize is `True`
		"""
		if not self.driver_address:
			frappe.throw(_("Cannot Calculate Arrival Time as Driver Address is Missing."))

		home_address = get_address_display(frappe.get_doc("Address", self.driver_address).as_dict())

		route_list = []
		# Initialize first leg with origin as the home address
		leg = [home_address]

		for stop in self.delivery_stops:
			leg.append(stop.customer_address)

			if optimize and stop.lock:
				route_list.append(leg)
				leg = [stop.customer_address]

		# For last leg, append home address as the destination
		# only if lock isn't on the final stop
		if len(leg) > 1:
			leg.append(home_address)
			route_list.append(leg)

		route_list = [[sanitize_address(address) for address in route] for route in route_list]

		return route_list

	def rearrange_stops(self, optimized_order, start):
		"""
		Re-arrange delivery stops based on order optimized
		for vehicle routing problems.

		Args:
			optimized_order (list of int): The index-based optimized order of the route
			start (int): The index at which to start the rearrangement
		"""

		stops_order = []

		# Child table idx starts at 1
		for new_idx, old_idx in enumerate(optimized_order, 1):
			new_idx = start + new_idx
			old_idx = start + old_idx

			self.delivery_stops[old_idx].idx = new_idx
			stops_order.append(self.delivery_stops[old_idx])

		self.delivery_stops[start:start + len(stops_order)] = stops_order

	def get_directions(self, route, optimize):
		"""
		Retrieve map directions for a given route and departure time.
		If optimize is `True`, Google Maps will return an optimized
		order for the intermediate waypoints.

		NOTE: Google's API does take an additional `departure_time` key,
		but it only works for routes without any waypoints.

		Args:
			route (list of str): Route addresses (origin -> waypoint(s), if any -> destination)
			optimize (bool): `True` if route needs to be optimized, else `False`

		Returns:
			(dict): Route legs and, if `optimize` is `True`, optimized waypoint order
		"""
		if not frappe.db.get_single_value("Google Settings", "api_key"):
			frappe.throw(_("Enter API key in Google Settings."))

		import googlemaps

		try:
			maps_client = googlemaps.Client(key=frappe.db.get_single_value("Google Settings", "api_key"))
		except Exception as e:
			frappe.throw(e)

		directions_data = {
			"origin": route[0],
			"destination": route[-1],
			"waypoints": route[1: -1],
			"optimize_waypoints": optimize
		}

		try:
			directions = maps_client.directions(**directions_data)
		except Exception as e:
			frappe.throw(_(str(e)))

		return directions[0] if directions else False


@frappe.whitelist()
def get_delivery_window(doctype=None, docname=None, customer=None):
	"""
	Fetch the set delivery window times for a Customer, or
	fallback to global defaults in Delivery Settings

	Args:
		doctype (str, optional): The transaction DocType in which the delivery window is set. Defaults to None.
		docname (str, optional): The transaction record in which the delivery window is set. Defaults to None.
		customer (str, optional): The name of the Customer. Defaults to None.

	Returns:
		frappe._dict: The dict object containing the window times,
			and a flag if the global defaults were picked up instead
	"""

	delivery_start_time = delivery_end_time = None
	default_window = False

	if doctype and docname:
		delivery_start_time, delivery_end_time = frappe.db.get_value(doctype, docname,
			["delivery_start_time", "delivery_end_time"])
	elif customer:
		delivery_start_time, delivery_end_time = frappe.db.get_value("Customer", customer,
			["delivery_start_time", "delivery_end_time"])

	if not (delivery_start_time and delivery_end_time):
		delivery_start_time = frappe.db.get_single_value("Delivery Settings", "delivery_start_time")
		delivery_end_time = frappe.db.get_single_value("Delivery Settings", "delivery_end_time")
		default_window = True

	return frappe._dict({
		"delivery_start_time": delivery_start_time,
		"delivery_end_time": delivery_end_time,
		"default_window": default_window
	})


@frappe.whitelist()
def get_contact_and_address(name):
	out = frappe._dict()

	get_default_contact(out, name)
	get_default_address(out, name)

	return out


def get_default_contact(out, name):
	contact_persons = frappe.db.sql(
		"""
			SELECT parent,
				(SELECT is_primary_contact FROM tabContact c WHERE c.name = dl.parent) AS is_primary_contact
			FROM
				`tabDynamic Link` dl
			WHERE
				dl.link_doctype="Customer"
				AND dl.link_name=%s
				AND dl.parenttype = "Contact"
		""", (name), as_dict=1)

	if contact_persons:
		for out.contact_person in contact_persons:
			if out.contact_person.is_primary_contact:
				return out.contact_person

		out.contact_person = contact_persons[0]

		return out.contact_person


def get_default_address(out, name):
	shipping_addresses = frappe.db.sql(
		"""
			SELECT parent,
				(SELECT is_shipping_address FROM tabAddress a WHERE a.name=dl.parent) AS is_shipping_address
			FROM
				`tabDynamic Link` dl
			WHERE
				dl.link_doctype="Customer"
				AND dl.link_name=%s
				AND dl.parenttype = "Address"
		""", (name), as_dict=1)

	if shipping_addresses:
		for out.shipping_address in shipping_addresses:
			if out.shipping_address.is_shipping_address:
				return out.shipping_address

		out.shipping_address = shipping_addresses[0]

		return out.shipping_address


@frappe.whitelist()
def get_contact_display(contact):
	contact_info = frappe.db.get_value(
		"Contact", contact,
		["first_name", "last_name", "phone", "mobile_no"],
		as_dict=1)

	contact_info.html = """ <b>%(first_name)s %(last_name)s</b> <br> %(phone)s <br> %(mobile_no)s""" % {
		"first_name": contact_info.first_name,
		"last_name": contact_info.last_name or "",
		"phone": contact_info.phone or "",
		"mobile_no": contact_info.mobile_no or ""
	}

	return contact_info.html


def sanitize_address(address):
	"""
	Remove HTML breaks in a given address

	Args:
		address (str): Address to be sanitized

	Returns:
		(str): Sanitized address
	"""

	if not address:
		return

	address = address.split('<br>')

	# Only get the first 3 blocks of the address
	return ', '.join(address[:3])


@frappe.whitelist()
def validate_unique_delivery_notes(delivery_stops):
	delivery_stops = json.loads(delivery_stops)
	delivery_notes = [stop.get("delivery_note") for stop in delivery_stops
		if stop.get("delivery_note")]

	if not delivery_notes:
		return []

	existing_trips = frappe.get_all("Delivery Stop",
		filters={
			"delivery_note": ["IN", delivery_notes],
			"docstatus": ["<", 2]
		},
		fields=["parent"],
		distinct=True)

	existing_trips = [stop.parent for stop in existing_trips]

	return existing_trips


@frappe.whitelist()
def notify_customers(delivery_trip):
	delivery_trip = frappe.get_doc("Delivery Trip", delivery_trip)

	context = delivery_trip.as_dict()

	if delivery_trip.driver:
		context.update({"cell_number": frappe.db.get_value("Driver", delivery_trip.driver, "cell_number")})

	email_recipients = []

	for stop in delivery_trip.delivery_stops:
		contact_info = frappe.db.get_value("Contact", stop.contact, ["first_name", "last_name", "email_id"], as_dict=1)

		context.update({"items": []})
		if stop.delivery_note:
			items = frappe.get_all("Delivery Note Item", filters={"parent": stop.delivery_note, "docstatus": 1}, fields=["*"])
			context.update({"items": items})

		if contact_info and contact_info.email_id:
			context.update(stop.as_dict())
			context.update(contact_info)

			dispatch_template_name = frappe.db.get_single_value("Delivery Settings", "dispatch_template")
			dispatch_template = frappe.get_doc("Email Template", dispatch_template_name)

			frappe.sendmail(recipients=contact_info.email_id,
				subject=dispatch_template.subject,
				message=frappe.render_template(dispatch_template.response, context),
				attachments=get_attachments(stop))

			stop.db_set("email_sent_to", contact_info.email_id)
			email_recipients.append(contact_info.email_id)

	if email_recipients:
		frappe.msgprint(_("Email sent to {0}").format(", ".join(email_recipients)))
		delivery_trip.db_set("email_notification_sent", True)
	else:
		frappe.msgprint(_("No contacts with email IDs found."))


def get_attachments(delivery_stop):
	if not (frappe.db.get_single_value("Delivery Settings", "send_with_attachment") and delivery_stop.delivery_note):
		return []

	dispatch_attachment = frappe.db.get_single_value("Delivery Settings", "dispatch_attachment")
	attachments = frappe.attach_print("Delivery Note", delivery_stop.delivery_note,
		file_name="Delivery Note", print_format=dispatch_attachment)

	return [attachments]


@frappe.whitelist()
def get_driver_email(driver):
	employee = frappe.db.get_value("Driver", driver, "employee")
	email = frappe.db.get_value("Employee", employee, "prefered_email")
	return {"email": email}


@frappe.whitelist()
def create_or_update_timesheet(trip, action, odometer_value=None):
	delivery_trip = frappe.get_doc("Delivery Trip", trip)
	time = frappe.utils.now()

	def get_timesheet():
		timesheet_list = frappe.get_all("Timesheet", filters={'docstatus': 0, 'delivery_trip': delivery_trip.name})
		if timesheet_list:
			return frappe.get_doc("Timesheet", timesheet_list[0].name)

	if action == "start":
		employee = frappe.get_value("Driver", delivery_trip.driver, "employee")
		timesheet = frappe.new_doc("Timesheet")
		timesheet.company = delivery_trip.company
		timesheet.employee = employee
		timesheet.delivery_trip = delivery_trip.name
		timesheet.append("time_logs", {
			"from_time": time,
			"activity_type": frappe.db.get_single_value("Delivery Settings", "default_activity_type")
		})
		timesheet.save()

		frappe.db.set_value("Delivery Trip", trip, "status", "In Transit", update_modified=False)  # Because we can't set status as allow on submit
		frappe.db.set_value("Delivery Trip", trip, "odometer_start_value", odometer_value, update_modified=False)
		frappe.db.set_value("Delivery Trip", trip, "odometer_start_time", time, update_modified=False)
	elif action == "pause":
		timesheet = get_timesheet()

		if timesheet and len(timesheet.time_logs) > 0:
			last_timelog = timesheet.time_logs[-1]

			if last_timelog.activity_type == frappe.db.get_single_value("Delivery Settings", "default_activity_type"):
				if last_timelog.from_time and not last_timelog.to_time:
					last_timelog.to_time = time
					timesheet.save()

		frappe.db.set_value("Delivery Trip", trip, "status", "Paused", update_modified=False)
	elif action == "continue":
		timesheet = get_timesheet()

		if timesheet and len(timesheet.time_logs) > 0:
			last_timelog = timesheet.time_logs[-1]

			if last_timelog.activity_type == frappe.db.get_single_value("Delivery Settings", "default_activity_type"):
				if last_timelog.from_time and last_timelog.to_time:
					timesheet.append("time_logs", {
						"from_time": time,
						"activity_type": frappe.db.get_single_value("Delivery Settings", "default_activity_type")
					})
					timesheet.save()

		frappe.db.set_value("Delivery Trip", trip, "status", "In Transit", update_modified=False)
	elif action == "end":
		timesheet = get_timesheet()

		if timesheet and len(timesheet.time_logs) > 0:
			last_timelog = timesheet.time_logs[-1]

			if last_timelog.activity_type == frappe.db.get_single_value("Delivery Settings", "default_activity_type"):
				last_timelog.to_time = time
				timesheet.save()
				timesheet.submit()

		frappe.db.set_value("Delivery Trip", trip, "status", "Completed", update_modified=False)
		frappe.db.set_value("Delivery Trip", trip, "odometer_end_value", odometer_value, update_modified=False)
		frappe.db.set_value("Delivery Trip", trip, "odometer_end_time", time, update_modified=False)
		
		actual_distance_travelled = flt(odometer_value) - flt(delivery_trip.odometer_start_value)
		frappe.db.set_value("Delivery Trip", trip, "actual_distance_travelled", actual_distance_travelled, update_modified=False)


@frappe.whitelist()
def make_payment_entry(payment_amount, sales_invoice):
	payment_entry = frappe._dict()
	if flt(payment_amount) > 0:
		payment_entry = get_payment_entry("Sales Invoice", sales_invoice, party_amount=flt(payment_amount))
		payment_entry.paid_amount = payment_amount
		payment_entry.reference_date = today()
		payment_entry.reference_no = sales_invoice
		payment_entry.flags.ignore_permissions = True
		payment_entry.save()

	update_delivery_trip_status(payment_amount, sales_invoice)

	return payment_entry.name


@frappe.whitelist()
def update_payment_due_date(sales_invoice):
	invoice = frappe.get_doc("Sales Invoice", sales_invoice)

	if not invoice.payment_terms_template:
		return

	due_date = get_due_date(invoice.posting_date, "Customer", invoice.customer, bill_date=frappe.utils.add_days(nowdate(), 7))

	# Update due date in both parent and child documents
	invoice.due_date = due_date
	for term in invoice.payment_schedule:
		term.due_date = due_date

	invoice.save()


def update_delivery_trip_status(payment_amount, sales_invoice):
	delivery_stops = frappe.get_all("Delivery Stop",
		filters={"sales_invoice": sales_invoice, "docstatus": 1},
		fields=["parent", "name"])

	delivery_trips = unique([stop.parent for stop in delivery_stops])
	delivery_stops = unique([stop.name for stop in delivery_stops])

	for trip in delivery_trips:
		trip_doc = frappe.get_doc("Delivery Trip", trip)
		for stop in trip_doc.delivery_stops:
			if stop.name in delivery_stops:
				stop.visited = True
				stop.paid_amount = payment_amount
				if stop.delivery_note:
					frappe.db.set_value("Delivery Note", stop.delivery_note, "status", "Completed")
		trip_doc.save()
