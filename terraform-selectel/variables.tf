variable "region" { type = string }
variable "availability_zone" { type = string }
variable "image_id" { type = string }
variable "volume_type" { type = string }
variable "flavor_ids" { type = object({ app = string, db = string }) }
variable "external_network_name" {
  type    = string
  default = "external-network"
}
variable "name_prefix" {
  type    = string
  default = "otus-dz06"
  validation {
    condition     = startswith(var.name_prefix, "otus-dz06")
    error_message = "Resources must use the DZ06 prefix."
  }
}
variable "subnet_cidr" {
  type    = string
  default = "10.66.0.0/24"
}
variable "admin_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrnetmask(c)) && split("/", c)[1] == "32"])
    error_message = "Provide operator IPv4 /32 addresses."
  }
}
variable "ssh_public_key_path" {
  type    = string
  default = "../.local/otus_dz06.pub"
}
variable "boot_disk_gb" {
  type    = number
  default = 20
}
variable "route_probe_key" {
  type      = string
  sensitive = true
}
