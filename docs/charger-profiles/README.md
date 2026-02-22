# Charger Profiles

This directory contains setup guides for specific EV charger models. Each guide describes how to connect a charger to the EV Charging Manager integration.

## Available Profiles

- [go-e Charger (Gemini / Gemini flex)](goe-api2.md)

## Contributing a New Profile

If you have a charger that works with EV Charging Manager, please consider contributing a setup guide. Create a new markdown file in this directory following the template below, and submit a pull request.

### Template

```markdown
# [Charger Model] Setup Guide

## Charger Model
- Model name and variants
- Required HA integration (with link)

## Network Prerequisites
- Connection method (WiFi, Ethernet, etc.)
- Any special network configuration

## HA Integration Installation
- Installation steps
- Recommended configuration (WebSocket vs polling, etc.)

## Required Entities

| Function | Entity Pattern | Notes |
|----------|---------------|-------|
| Car status | sensor.xxx_status | Values: idle, charging, etc. |
| Session energy | sensor.xxx_energy | Unit: kWh |
| Power | sensor.xxx_power | Unit: W |
| Total energy | sensor.xxx_total_energy | Optional, for cross-validation |
| RFID | sensor.xxx_rfid | Optional, for user identification |

## RFID Configuration (if applicable)
- How to program RFID cards
- How RFID values map to card indices

## EV Charging Manager Config Flow
- Which profile to select (or "Other / Manual configuration")
- Any special entity mapping notes

## Known Quirks
- Any charger-specific behaviors or limitations
```

### Guidelines

- One file per charger model (or family of models)
- Use the entity patterns from your HA integration
- Include real examples with placeholder serial numbers
- Document any charger-specific sensor behaviors
- Reference the go-e guide ([goe-api2.md](goe-api2.md)) as an example
