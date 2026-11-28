//*****************************************************************************
// BeamNG.Earth: the precipitation vertex shader with a near fade (weather phase B). The stock precipV.hlsl fades the
// drops only far away (fadeStartEnd); a drop passing right in front of the camera was drawn at full opacity, a big
// soft blob (the "blob drops" of the broker shots). Here a drop also fades out from NEAR_END to NEAR_START metres.
// A new file under a new ShaderData name (bngEarthPrecipNearShader, weather.lua): no stock file is overridden.
//*****************************************************************************

#include "../common/precip.h.hlsl"

#define NEAR_START 0.8
#define NEAR_END 2.5

struct Vert
{
    float4 position    : POSITION;
    float4 texCoord    : TEXCOORD0;
};

Conn main(Vert In)
{
    Conn Out;

    Out.position = mul(modelview, In.position);
    Out.texCoord = In.texCoord;
    Out.color = float4(ambient.r, ambient.g, ambient.b, 1);

    float distance = length(cameraPos - In.position.xyz);
    if (fadeStartEnd.x < fadeStartEnd.y) {
        Out.color.a = abs(clamp((distance - fadeStartEnd.x) / (fadeStartEnd.y - fadeStartEnd.x), 0, 1) - 1);
    }
    Out.color.a *= saturate((distance - NEAR_START) / (NEAR_END - NEAR_START));

    return Out;
}
