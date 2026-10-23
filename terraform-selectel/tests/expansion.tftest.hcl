# Offline provider mock: no cloud credentials, API calls or real infrastructure.
mock_provider "openstack" {}

variables {
  name_prefix       = "otus-dz06"
  subnet_cidr       = "10.66.0.0/24"
  region            = "ru-7"
  availability_zone = "ru-7a"
  image_id          = "00000000-0000-4000-8000-000000000001"
  volume_type       = "synthetic-volume-type"
  flavor_ids = {
    app = "00000000-0000-4000-8000-000000000002"
    db  = "00000000-0000-4000-8000-000000000003"
  }
  admin_cidrs         = ["192.0.2.1/32"]
  ssh_public_key_path = "tests/mock-public-key.txt"
  route_probe_key     = "mock-test-token-with-no-real-access"
  nodes = {
    app = { host = 10, role = "app" }
    db1 = { host = 21, role = "db" }
    db2 = { host = 22, role = "db" }
    db3 = { host = 23, role = "db" }
  }
  etcd_members = ["db1", "db2", "db3"]
}

run "initial" {
  command = apply
  assert {
    condition = openstack_networking_subnet_v2.lab.enable_dhcp && alltrue([
      for name, port in openstack_networking_port_v2.node :
      one(port.fixed_ip).ip_address == cidrhost(var.subnet_cidr, var.nodes[name].host)
    ])
    error_message = "DHCP must provide the IPv4 reserved on each Neutron port."
  }
  assert {
    condition = alltrue([for name, node in openstack_compute_instance_v2.node :
      alltrue([for file in yamldecode(node.user_data).write_files : !startswith(file.path, "/etc/netplan/")]) &&
      alltrue([for command in yamldecode(node.user_data).runcmd : !strcontains(command, "netplan")]) &&
      yamldecode(node.user_data).runcmd[1] == "python3 /root/${var.name_prefix}-check-ipv4.py ${cidrhost(var.subnet_cidr, var.nodes[name].host)}/${split("/", var.subnet_cidr)[1]} ${openstack_networking_subnet_v2.lab.gateway_ip} || { python3 /root/${var.name_prefix}-network-diagnostics.py; exit 1; }" &&
      length([for file in yamldecode(node.user_data).write_files : file if file.path == "/root/${var.name_prefix}-network-diagnostics.py" && file.permissions == "0600"]) == 1
    ])
    error_message = "Cloud networking must be preserved and IPv4 checked before starting services."
  }
  assert {
    condition = alltrue([for node in openstack_compute_instance_v2.node :
      yamldecode(node.user_data).runcmd[0] == "set -eu" &&
      alltrue([for command in yamldecode(node.user_data).runcmd : !strcontains(command, "&&")])
    ])
    error_message = "Bootstrap must stop on a failed command instead of hiding it behind a successful probe start."
  }
  assert {
    condition = alltrue([for node in openstack_compute_instance_v2.node :
      alltrue([for file in yamldecode(node.user_data).write_files : !startswith(file.path, "/etc/ssh/")]) &&
      alltrue([for command in yamldecode(node.user_data).runcmd : !strcontains(command, "ssh.service") && !strcontains(command, "ssh.socket")])
    ])
    error_message = "The image must retain control of SSH configuration and activation."
  }
  assert {
    condition     = join(",", openstack_networking_network_v2.lab.availability_zone_hints) == var.availability_zone && join(",", openstack_networking_router_v2.lab.availability_zone_hints) == var.availability_zone
    error_message = "Network and router must use the chosen segment."
  }
  assert {
    condition     = length(output.lab.server_ids) == 4
    error_message = "Initial topology must include app and three database nodes."
  }
}

run "expanded" {
  command = apply
  variables {
    nodes = {
      app = { host = 10, role = "app" }
      db1 = { host = 21, role = "db" }
      db2 = { host = 22, role = "db" }
      db3 = { host = 23, role = "db" }
      db4 = { host = 24, role = "db" }
    }
  }
  assert {
    condition     = length(output.lab.server_ids) == 5 && output.lab.private_ips.db4 == "10.66.0.24"
    error_message = "The new node must be present at its desired address."
  }
  assert {
    condition     = alltrue([for name, id in run.initial.lab.server_ids : output.lab.server_ids[name] == id])
    error_message = "Adding db4 must preserve existing server identities."
  }
  assert {
    condition     = output.lab.etcd_members == run.initial.lab.etcd_members
    error_message = "PostgreSQL expansion must preserve the etcd quorum."
  }
  assert {
    condition = alltrue(concat(
      [for name, id in run.initial.lab.volume_ids : output.lab.volume_ids[name] == id],
      [for name, id in run.initial.lab.port_ids : output.lab.port_ids[name] == id]
    ))
    error_message = "Adding db4 must preserve existing disks and ports."
  }
}

run "repeat_expanded" {
  command = apply
  variables {
    nodes = {
      app = { host = 10, role = "app" }
      db1 = { host = 21, role = "db" }
      db2 = { host = 22, role = "db" }
      db3 = { host = 23, role = "db" }
      db4 = { host = 24, role = "db" }
    }
  }
  assert {
    condition     = output.lab.server_ids == run.expanded.lab.server_ids && output.lab.volume_ids == run.expanded.lab.volume_ids && output.lab.port_ids == run.expanded.lab.port_ids
    error_message = "Repeat apply must preserve the expanded topology."
  }
}
